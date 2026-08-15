from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING

from engram.tokenizer import tokenize

if TYPE_CHECKING:  # 仅用于类型标注：运行时不加载向量与模型相关模块，
    from engram.vectors import VectorStore  # 保证纯写入路径的依赖面最小

EXCERPT_LIMIT = 200
RRF_K = 60


@dataclass(frozen=True, slots=True)
class SearchHit:
    record_id: str
    title: str
    excerpt: str
    score: float
    keyword_score: float = 0.0
    vector_score: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "title": self.title,
            "excerpt": self.excerpt,
            "score": round(self.score, 6),
            "keyword_score": round(self.keyword_score, 6),
            "vector_score": round(self.vector_score, 6),
        }


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

    def keyword(self, query: str, *, limit: int = 5) -> list[SearchHit]:
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
              AND r.status = 'active'
            ORDER BY rank
            LIMIT ?
            """,
            (expression, limit),
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

    def vector(self, query_vector: list[float], *, limit: int = 5) -> list[SearchHit]:
        if self.store is None:
            return []
        hits: list[SearchHit] = []
        for record_id, score in self.store.neighbors(query_vector, limit=limit):
            row = self.connection.execute(
                "SELECT title, body FROM records "
                "WHERE record_id = ? AND status = 'active'",
                (record_id,),
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

    def hybrid(
        self,
        query: str,
        query_vector: list[float],
        *,
        limit: int = 5,
    ) -> list[SearchHit]:
        """倒数排名融合（RRF）。

        两路各自按名次贡献 1/(K+rank)，不比较原始分数——关键词的 bm25
        与向量的余弦距离量纲不同，直接加权会让其中一路主导结果。
        """
        keyword_hits = self.keyword(query, limit=limit * 2)
        vector_hits = self.vector(query_vector, limit=limit * 2)
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
        results: list[SearchHit] = []
        for entry in ordered[:limit]:
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
                )
            )
        return results
