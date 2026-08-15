"""金标召回评测。

检索质量没有自然的回归信号：换嵌入模型、改分词、调融合权重之后，结果只是
"看起来还行"。这个模块把"还行"变成数字，并把数字钉在一组固定问题上。

锚点是 content_hash 而不是 record_id：record_id 每次迁移都重新生成，用它做锚
的金标只在生成它的那个库里有效。金标里的问题属于私有内容，因此金标文件放在
数据目录，不进仓库。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from engram.errors import InvalidInputError
from engram.search import SearchService

GOLD_SCHEMA_VERSION = 2
RECALL_CUTOFFS = (1, 3)


@dataclass(frozen=True, slots=True)
class GoldQuery:
    query_id: str
    query: str
    expected_content_hash: str


@dataclass(frozen=True, slots=True)
class GoldSet:
    path: Path
    queries: tuple[GoldQuery, ...]


@dataclass(frozen=True, slots=True)
class RecallReport:
    total: int
    top_k: int
    mode: str
    hits: dict[int, int]
    misses: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "total": self.total,
            "top_k": self.top_k,
            "mode": self.mode,
            "recall": {
                str(cutoff): {
                    "hits": count,
                    "rate": round(count / self.total, 4) if self.total else 0.0,
                }
                for cutoff, count in sorted(self.hits.items())
            },
            "misses": list(self.misses),
        }


def load_gold(path: Path) -> GoldSet:
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    version = payload.get("schema_version")
    if version != GOLD_SCHEMA_VERSION:
        # v1 锚在 (文件, 行号) 上。源文件重排之后行号全部错位，照单全收
        # 只会产出一个看起来正常的错误分数——比直接失败危险得多。
        raise InvalidInputError(
            "gold set must be rebased onto content_hash anchors",
            context={
                "path": str(path),
                "schema_version": version,
                "expected_schema_version": GOLD_SCHEMA_VERSION,
            },
        )
    return GoldSet(
        path=path,
        queries=tuple(
            GoldQuery(
                query_id=item["id"],
                query=item["query"],
                expected_content_hash=item["expected_content_hash"],
            )
            for item in payload.get("queries", ())
        ),
    )


def _resolve(search: SearchService, gold: GoldSet) -> dict[str, str]:
    """把金标锚点解析成当前库里的 record_id。"""
    wanted = {query.expected_content_hash for query in gold.queries}
    if not wanted:
        return {}
    # 只有占位符个数进入 SQL 文本，取值一律走参数绑定。
    placeholders = ",".join("?" * len(wanted))
    rows = search.connection.execute(
        "SELECT record_id, content_hash FROM records "
        f"WHERE content_hash IN ({placeholders})",
        tuple(wanted),
    ).fetchall()
    resolved = {row["content_hash"]: row["record_id"] for row in rows}
    unresolved = [
        query.query_id
        for query in gold.queries
        if query.expected_content_hash not in resolved
    ]
    if unresolved:
        raise InvalidInputError(
            "gold expectations are absent from the database",
            context={"path": str(gold.path), "unresolved": unresolved},
        )
    return resolved


def run_recall(
    *,
    gold: GoldSet,
    search: SearchService,
    top_k: int = 5,
    mode: str = "keyword",
    embed_query: Callable[[str], Sequence[float]] | None = None,
) -> RecallReport:
    if mode != "keyword" and embed_query is None:
        raise InvalidInputError(
            "vector and hybrid modes require an embedder",
            context={"mode": mode},
        )
    resolved = _resolve(search, gold)
    cutoffs = sorted({*RECALL_CUTOFFS, top_k})
    hits = dict.fromkeys(cutoffs, 0)
    misses: list[str] = []

    for query in gold.queries:
        if mode == "keyword":
            found = search.keyword(query.query, limit=top_k)
        else:
            vector = list(embed_query(query.query))  # type: ignore[misc]
            found = (
                search.vector(vector, limit=top_k)
                if mode == "vector"
                else search.hybrid(query.query, vector, limit=top_k)
            )
        target = resolved[query.expected_content_hash]
        rank = next(
            (
                index
                for index, hit in enumerate(found, start=1)
                if hit.record_id == target
            ),
            None,
        )
        if rank is None:
            misses.append(query.query_id)
            continue
        for cutoff in cutoffs:
            if rank <= cutoff:
                hits[cutoff] += 1

    return RecallReport(
        total=len(gold.queries),
        top_k=top_k,
        mode=mode,
        hits=hits,
        misses=tuple(misses),
    )
