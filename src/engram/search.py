from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import wraps
from typing import TYPE_CHECKING

from engram.current_memory import CurrentMemoryIndex
from engram.errors import InvalidInputError
from engram.tokenizer import tokenize

if TYPE_CHECKING:  # 仅用于类型标注：运行时不加载向量与模型相关模块，
    from engram.vectors import VectorStore  # 保证纯写入路径的依赖面最小

EXCERPT_LIMIT = 200
RRF_K = 60
LINK_MIN_SCORE = 0.5
LINK_EXPANSION_ALPHA = 0.5
LINK_EXPANSION_MAX_EDGES = 3
# sqlite-vec's installed KNN contract rejects k > 4096. This limits vector
# candidates only; an explicit correction chain is always followed in full.
VECTOR_MAX_CANDIDATES = 4096


def _consistent_read(method):
    """Read bodies and correction relations from one SQLite snapshot."""

    @wraps(method)
    def snapshot(self, *args, **kwargs):
        if self.connection.in_transaction:
            return method(self, *args, **kwargs)
        self.connection.execute("BEGIN")
        try:
            return method(self, *args, **kwargs)
        finally:
            self.connection.execute("ROLLBACK")

    return snapshot


@dataclass(frozen=True, slots=True)
class SearchHit:
    record_id: str
    title: str
    excerpt: str
    score: float
    keyword_score: float = 0.0
    vector_score: float = 0.0
    current: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "record_id": self.record_id,
            "title": self.title,
            "excerpt": self.excerpt,
            "score": round(self.score, 6),
            "keyword_score": round(self.keyword_score, 6),
            "vector_score": round(self.vector_score, 6),
        }
        if self.current is not None:
            payload["current"] = self.current
        return payload


def _fts_query(query: str) -> str:
    """构造 FTS 查询表达式。

    索引侧同时保留中文单字与二元组，但查询侧只用长度大于 1 的 token：
    单字经 OR 连接会让"量子色动力学"匹配到"认知工效学"（同含"学"），
    精度无法接受。仅当查询本身短到没有二元组时才回退到单字。
    """
    tokens = tokenize(query)
    if not tokens:
        return ""
    multi = [token for token in tokens if len(token) > 1]
    selected = multi or tokens
    escaped = [token.replace('"', '""') for token in selected]
    return " OR ".join(f'"{token}"' for token in escaped)


class SearchService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        store: VectorStore | None = None,
    ) -> None:
        self.connection = connection
        self.store = store

    @_consistent_read
    def current_record(
        self, record_id: str, *, view: str = "current"
    ) -> dict[str, object] | None:
        """Describe applicability without changing get's historical source body."""
        self._validate_view(view)
        return CurrentMemoryIndex(self.connection).describe(record_id, view=view)

    @staticmethod
    def _validate_view(view: str) -> None:
        if view not in ("current", "history"):
            raise InvalidInputError("view must be current or history")

    def _rank(
        self, hits: list[SearchHit], *, projects: Sequence[str] | None, query: str = ""
    ) -> list[SearchHit]:
        wanted = {
            project.casefold().strip()
            for project in (projects or [])
            if project.strip()
        }
        query_lower = query.casefold()

        def relevance(hit: SearchHit) -> tuple[float, int]:
            known = (
                {
                    project.casefold()
                    for project in (
                        hit.current.get("projects", [])
                        if hit.current
                        else [
                            row[0]
                            for row in self.connection.execute(
                                "SELECT project FROM record_projects WHERE record_id = ?",
                                (hit.record_id,),
                            )
                        ]
                    )
                }
                if wanted
                else set()
            )
            # Project context is a finite preference, never a membership filter or
            # an absolute ordering tier. The current query has a stronger signal
            # than a different cwd hint, without displacing strong direct matches.
            explicit = any(project and project in query_lower for project in known)
            related = bool(known & wanted)
            corrected = bool(hit.current and hit.current.get("correction_chain"))
            # Corrections receive a small tie-breaking preference within relevant
            # candidates; an unrelated weak vector/graph hit must not beat a strong
            # direct match merely because it carries a correction chain.
            return (
                hit.score
                * (
                    1.0
                    + 0.10 * explicit
                    + 0.05 * (related and not explicit)
                    + 0.05 * corrected
                ),
                int(corrected),
            )

        return sorted(hits, key=relevance, reverse=True)

    def _resolve(
        self, hits: list[SearchHit], index: CurrentMemoryIndex, *, view: str
    ) -> list[SearchHit]:
        resolved: dict[str, SearchHit] = {}
        for hit in hits:
            if view == "history":
                described = index.describe(hit.record_id, view=view)
                value = replace(
                    hit,
                    current={
                        **(described or {}),
                        "body": index.record(hit.record_id)["body"],
                        "body_role": "source_original",
                        "revision": index.record(hit.record_id)["revision"],
                    },
                )
                resolved[value.record_id] = value
                continue
            for projection in index.project(hit.record_id):
                has_chain = bool(projection.payload["correction_chain"])
                value = replace(
                    hit,
                    record_id=projection.record_id,
                    title=projection.title,
                    excerpt=projection.current_text[:EXCERPT_LIMIT],
                    current=projection.payload if has_chain else None,
                )
                existing = resolved.get(value.record_id)
                if existing is None or value.score > existing.score:
                    resolved[value.record_id] = value
        return list(resolved.values())

    @_consistent_read
    def keyword(
        self,
        query: str,
        *,
        limit: int = 5,
        projects: Sequence[str] | None = None,
        view: str = "current",
    ) -> list[SearchHit]:
        self._validate_view(view)
        index = CurrentMemoryIndex(self.connection)
        candidates = self._keyword(
            query,
            limit=None
            if index.has_corrections or projects or view == "history"
            else limit,
            include_history=index.has_corrections or view == "history",
        )
        return self._rank(
            self._resolve(candidates, index, view=view), projects=projects, query=query
        )[:limit]

    def _keyword(
        self, query: str, *, limit: int | None, include_history: bool = False
    ) -> list[SearchHit]:
        expression = _fts_query(query)
        if not expression:
            return []
        rows = self.connection.execute(
            """
            SELECT f.record_id AS record_id,
                   r.title AS title,
                   r.body AS body,
                   bm25(records_fts) AS rank
            FROM records_fts AS f
            JOIN records AS r ON r.record_id = f.record_id
            WHERE records_fts MATCH ?
              AND (? OR r.status = 'active')
            ORDER BY rank
            LIMIT ?
            """,
            (expression, include_history, limit if limit is not None else -1),
        ).fetchall()
        hits: list[SearchHit] = []
        for row in rows:
            score = 1.0 / (1.0 + max(row["rank"], 0.0))
            hits.append(
                SearchHit(
                    record_id=row["record_id"],
                    title=row["title"],
                    excerpt=row["body"][:EXCERPT_LIMIT],
                    score=score,
                    keyword_score=score,
                )
            )
        return hits

    @_consistent_read
    def vector(
        self,
        query_vector: list[float],
        *,
        limit: int = 5,
        projects: Sequence[str] | None = None,
        view: str = "current",
    ) -> list[SearchHit]:
        self._validate_view(view)
        index = CurrentMemoryIndex(self.connection)
        candidate_limit = (
            self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if index.has_corrections or projects or view == "history"
            else limit
        )
        hits = self._vector(
            query_vector,
            limit=candidate_limit,
            include_history=index.has_corrections or view == "history",
        )
        return self._rank(self._resolve(hits, index, view=view), projects=projects)[
            :limit
        ]

    def _vector(
        self, query_vector: list[float], *, limit: int, include_history: bool = False
    ) -> list[SearchHit]:
        if self.store is None:
            return []
        if limit <= 0:
            return []
        hits: list[SearchHit] = []
        for record_id, score in self.store.neighbors(
            query_vector, limit=min(limit, VECTOR_MAX_CANDIDATES)
        ):
            row = self.connection.execute(
                "SELECT title, body FROM records "
                "WHERE record_id = ? AND (? OR status = 'active')",
                (record_id, include_history),
            ).fetchone()
            if row is None:
                continue
            hits.append(
                SearchHit(
                    record_id=record_id,
                    title=row["title"],
                    excerpt=row["body"][:EXCERPT_LIMIT],
                    score=score,
                    vector_score=score,
                )
            )
        return hits

    @_consistent_read
    def hybrid(
        self,
        query: str,
        query_vector: list[float],
        *,
        limit: int = 5,
        projects: Sequence[str] | None = None,
        view: str = "current",
    ) -> list[SearchHit]:
        """倒数排名融合（RRF）。

        两路各自按名次贡献 1/(K+rank)，不比较原始分数——关键词的 bm25
        与向量的余弦距离量纲不同，直接加权会让其中一路主导结果。
        """
        self._validate_view(view)
        index = CurrentMemoryIndex(self.connection)
        candidate_limit = (
            self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            if index.has_corrections or projects or view == "history"
            else limit * 2
        )
        include_history = index.has_corrections or view == "history"
        keyword_hits = self._resolve(
            self._keyword(
                query, limit=candidate_limit, include_history=include_history
            ),
            index,
            view=view,
        )
        vector_hits = self._resolve(
            self._vector(
                query_vector, limit=candidate_limit, include_history=include_history
            ),
            index,
            view=view,
        )
        fused: dict[str, dict[str, object]] = {}
        for rank, hit in enumerate(keyword_hits, start=1):
            entry = fused.setdefault(
                hit.record_id, {"hit": hit, "score": 0.0, "kw": 0.0, "vec": 0.0}
            )
            entry["score"] = float(entry["score"]) + 1.0 / (RRF_K + rank)
            entry["kw"] = hit.keyword_score
        for rank, hit in enumerate(vector_hits, start=1):
            entry = fused.setdefault(
                hit.record_id, {"hit": hit, "score": 0.0, "kw": 0.0, "vec": 0.0}
            )
            entry["score"] = float(entry["score"]) + 1.0 / (RRF_K + rank)
            entry["vec"] = hit.vector_score
        ordered = sorted(
            fused.values(), key=lambda item: float(item["score"]), reverse=True
        )
        seed_ids = [
            entry["hit"].record_id
            for entry in ordered[:limit]
            if isinstance(entry["hit"], SearchHit)
        ]
        # Related edges attached to an explicitly superseded source still lead to
        # the current replacement. Correction closure precedes the one-hop graph
        # expansion; every expanded target is resolved through that same closure.
        for entry in ordered[:limit]:
            base = entry["hit"]
            if isinstance(base, SearchHit) and base.current:
                seed_ids.extend(
                    str(item["record_id"]) for item in base.current["correction_chain"]
                )
        seed_ids = list(dict.fromkeys(seed_ids))
        if seed_ids:
            self._expand_along_links(fused, seed_ids, index=index, view=view)
            ordered = sorted(
                fused.values(), key=lambda item: float(item["score"]), reverse=True
            )
        results: list[SearchHit] = []
        for entry in ordered:
            base = entry["hit"]
            if not isinstance(base, SearchHit):
                continue
            results.append(
                SearchHit(
                    record_id=base.record_id,
                    title=base.title,
                    excerpt=base.excerpt,
                    score=float(entry["score"]),
                    keyword_score=float(entry["kw"]),
                    vector_score=float(entry["vec"]),
                    current=base.current,
                )
            )
        return self._rank(results, projects=projects, query=query)[:limit]

    def _expand_along_links(
        self,
        fused: dict[str, dict[str, object]],
        seed_ids: list[str],
        *,
        index: CurrentMemoryIndex,
        view: str,
    ) -> None:
        """沿高质量边把近邻捞回候选集（一跳扩展）。

        建边发生在写入期、检索从不读取的话，链接网络就只是摆设——这里让它
        参与检索。贡献刻意压低（alpha * link_score / RRF_K ≈ 单通道第 3 名），
        能捞回邻居但压不过双通道直接命中。单条批量 SQL，无 N+1。
        """
        placeholders = ",".join("?" for _ in seed_ids)
        rows = self.connection.execute(
            f"""
            SELECT l.source_id AS source_id,
                   l.target_id AS target_id,
                   l.score AS link_score,
                   r.title AS title,
                   r.body AS body
            FROM record_links AS l
            JOIN records AS r ON r.record_id = l.target_id
            WHERE l.source_id IN ({placeholders})
              AND l.relation = 'related_to'
              AND l.score >= ?
            ORDER BY l.source_id, l.score DESC
            """,
            (*seed_ids, LINK_MIN_SCORE),
        ).fetchall()
        used: dict[str, int] = {}
        for row in rows:
            source = row["source_id"]
            contribution = LINK_EXPANSION_ALPHA * float(row["link_score"]) / RRF_K
            neighbors = self._resolve(
                [
                    SearchHit(
                        record_id=row["target_id"],
                        title=row["title"],
                        excerpt=row["body"][:EXCERPT_LIMIT],
                        score=0.0,
                        vector_score=float(row["link_score"]),
                    )
                ],
                index,
                view=view,
            )
            if not neighbors or used.get(source, 0) >= LINK_EXPANSION_MAX_EDGES:
                continue
            used[source] = used.get(source, 0) + 1
            for neighbor in neighbors:
                entry = fused.get(neighbor.record_id)
                if entry is not None:
                    entry["score"] = float(entry["score"]) + contribution
                    continue
                fused[neighbor.record_id] = {
                    "hit": neighbor,
                    "score": contribution,
                    "kw": 0.0,
                    "vec": float(row["link_score"]),
                }
