from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from engram.tokenizer import tokenize

EXCERPT_LIMIT = 200


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
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

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
