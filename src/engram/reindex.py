"""Stage a complete embedding generation before replacing the live vector index."""

from __future__ import annotations

import sqlite3

import sqlite_vec

from engram.db import write_transaction
from engram.embedding import Embedder, from_blob, to_blob
from engram.links import build_links
from engram.vectors import VectorStore


def rebuild(connection: sqlite3.Connection, embedder: Embedder) -> dict[str, int]:
    rows = connection.execute(
        "SELECT record_id,title,body,content_hash FROM records ORDER BY record_id"
    ).fetchall()
    identity = [(row["record_id"], row["content_hash"]) for row in rows]
    connection.execute("DROP TABLE IF EXISTS temp.engram_reindex_stage")
    connection.execute(
        "CREATE TEMP TABLE engram_reindex_stage("
        "record_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, embedding BLOB NOT NULL)"
    )
    try:
        for row in rows:
            vector = embedder.embed([f"{row['title']}\n{row['body']}"])[0]
            if len(vector) != embedder.dimensions:
                raise ValueError("rebuild embedding dimension mismatch")
            connection.execute(
                "INSERT INTO temp.engram_reindex_stage VALUES (?,?,?)",
                (row["record_id"], row["content_hash"], to_blob(vector)),
            )

        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        with write_transaction(connection) as tx:
            current = [
                (row["record_id"], row["content_hash"])
                for row in tx.execute(
                    "SELECT record_id,content_hash FROM records ORDER BY record_id"
                )
            ]
            if current != identity:
                raise RuntimeError("records changed during rebuild; retry required")
            tx.execute("DROP TABLE IF EXISTS vec_records")
            tx.execute(
                "CREATE VIRTUAL TABLE vec_records USING vec0("
                "record_id TEXT PRIMARY KEY, "
                f"embedding float[{embedder.dimensions}])"
            )
            tx.execute("DELETE FROM embeddings")
            tx.execute(
                "INSERT INTO embeddings(record_id,model,dimensions,generation,"
                "input_hash,embedding,created_at) "
                "SELECT record_id,?,?,?,input_hash,embedding,datetime('now') "
                "FROM temp.engram_reindex_stage",
                (
                    embedder.model,
                    embedder.dimensions,
                    f"{embedder.model}-{embedder.dimensions}",
                ),
            )
            for staged in tx.execute(
                "SELECT record_id,embedding FROM temp.engram_reindex_stage"
            ):
                tx.execute(
                    "INSERT INTO vec_records(record_id,embedding) VALUES (?,?)",
                    (staged["record_id"], staged["embedding"]),
                )
            tx.execute("DELETE FROM record_links WHERE provenance='knn'")

        store = VectorStore(connection, dimensions=embedder.dimensions)
        linked = 0
        for row in rows:
            vector = from_blob(
                connection.execute(
                    "SELECT embedding FROM embeddings WHERE record_id=?",
                    (row["record_id"],),
                ).fetchone()[0]
            )
            linked += build_links(
                connection,
                row["record_id"],
                store.neighbors(vector, limit=3, exclude=row["record_id"]),
            )
        return {"rebuilt": len(rows), "links_written": linked}
    finally:
        connection.execute("DROP TABLE IF EXISTS temp.engram_reindex_stage")
