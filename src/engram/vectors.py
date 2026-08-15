from __future__ import annotations

import sqlite3

import sqlite_vec

from engram.db import write_transaction
from engram.embedding import to_blob


class VectorStore:
    """派生向量层。

    权威向量以二进制 float32 存在 `embeddings.embedding`，只存一份；
    `vec_records` 是可随时重建的检索投影，不作为事实源。
    """

    def __init__(self, connection: sqlite3.Connection, *, dimensions: int) -> None:
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        self.connection = connection
        self.dimensions = dimensions
        self._load_extension()
        self._ensure_table()

    def _load_extension(self) -> None:
        self.connection.enable_load_extension(True)
        sqlite_vec.load(self.connection)
        self.connection.enable_load_extension(False)

    def _ensure_table(self) -> None:
        self.connection.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec_records "
            f"USING vec0(record_id TEXT PRIMARY KEY, "
            f"embedding float[{self.dimensions}])"
        )

    def put(
        self,
        record_id: str,
        vector: list[float],
        *,
        model: str,
        dimensions: int,
        generation: str,
        input_hash: str,
    ) -> None:
        if len(vector) != self.dimensions or dimensions != self.dimensions:
            raise ValueError(
                f"vector dimensions mismatch: store={self.dimensions}, "
                f"given={len(vector)}"
            )
        with write_transaction(self.connection) as tx:
            tx.execute(
                """
                INSERT INTO embeddings(
                    record_id, model, dimensions, generation,
                    input_hash, embedding, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(record_id) DO UPDATE SET
                    model = excluded.model,
                    dimensions = excluded.dimensions,
                    generation = excluded.generation,
                    input_hash = excluded.input_hash,
                    embedding = excluded.embedding
                """,
                (record_id, model, dimensions, generation, input_hash, to_blob(vector)),
            )
            tx.execute("DELETE FROM vec_records WHERE record_id = ?", (record_id,))
            tx.execute(
                "INSERT INTO vec_records(record_id, embedding) VALUES (?, ?)",
                (record_id, sqlite_vec.serialize_float32(vector)),
            )

    def neighbors(
        self,
        vector: list[float],
        *,
        limit: int = 5,
        exclude: str | None = None,
    ) -> list[tuple[str, float]]:
        if len(vector) != self.dimensions:
            raise ValueError("query vector dimensions do not match store")
        rows = self.connection.execute(
            """
            SELECT record_id, distance
            FROM vec_records
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
            """,
            (sqlite_vec.serialize_float32(vector), limit + (1 if exclude else 0)),
        ).fetchall()
        results: list[tuple[str, float]] = []
        for row in rows:
            if exclude is not None and row["record_id"] == exclude:
                continue
            results.append((row["record_id"], 1.0 / (1.0 + row["distance"])))
            if len(results) >= limit:
                break
        return results

    def count(self) -> int:
        return int(
            self.connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        )
