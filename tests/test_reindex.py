from pathlib import Path

import pytest

from engram.db import connect
from engram.domain import RecordDraft
from engram.embedding import DeterministicEmbedder
from engram.migrations import migrate
from engram.reindex import rebuild
from engram.repository import RecordRepository
from engram.vectors import VectorStore


def test_rebuild_changes_vector_dimensions_without_losing_records(tmp_path: Path):
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    repository = RecordRepository(connection)
    record = repository.create(RecordDraft(title="人因工程", body="工作负荷评估"))
    old = DeterministicEmbedder(dimensions=64)
    VectorStore(connection, dimensions=64).put(
        record.record_id,
        old.embed([f"{record.title}\n{record.body}"])[0],
        model=old.model,
        dimensions=64,
        generation="old",
        input_hash=record.content_hash,
    )

    result = rebuild(connection, DeterministicEmbedder(dimensions=128))

    assert result["rebuilt"] == 1
    assert repository.count() == 1
    assert connection.execute("SELECT dimensions FROM embeddings").fetchone()[0] == 128
    assert VectorStore(connection, dimensions=128).count() == 1


def test_failed_rebuild_keeps_old_vectors(tmp_path: Path):
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    repository = RecordRepository(connection)
    record = repository.create(RecordDraft(title="测试", body="保留旧向量"))
    old = DeterministicEmbedder(dimensions=64)
    VectorStore(connection, dimensions=64).put(
        record.record_id,
        old.embed(["测试\n保留旧向量"])[0],
        model=old.model,
        dimensions=64,
        generation="old",
        input_hash=record.content_hash,
    )

    class BrokenEmbedder:
        model = "broken"
        dimensions = 128

        def embed(self, texts):
            raise RuntimeError("model failed")

    with pytest.raises(RuntimeError, match="model failed"):
        rebuild(connection, BrokenEmbedder())

    assert connection.execute("SELECT dimensions FROM embeddings").fetchone()[0] == 64
    assert VectorStore(connection, dimensions=64).count() == 1
