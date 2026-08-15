from pathlib import Path

import pytest

from engram.db import connect
from engram.domain import RecordDraft
from engram.embedding import DeterministicEmbedder
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.vectors import VectorStore


@pytest.fixture()
def context(tmp_path: Path):
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    counter = iter(f"rec_{index:04d}" for index in range(1, 1000))
    repository = RecordRepository(
        connection,
        id_factory=lambda: next(counter),
        clock=lambda: "2026-08-15T00:00:00Z",
    )
    store = VectorStore(connection, dimensions=64)
    return repository, store, DeterministicEmbedder(dimensions=64)


def _store_record(repository, store, embedder, title: str, body: str):
    record = repository.create(RecordDraft(title=title, body=body))
    vector = embedder.embed([f"{title}\n{body}"])[0]
    store.put(
        record.record_id,
        vector,
        model=embedder.model,
        dimensions=64,
        generation="g1",
        input_hash=record.content_hash,
    )
    return record, vector


def test_put_then_find_self_as_nearest(context) -> None:
    repository, store, embedder = context
    record, vector = _store_record(
        repository, store, embedder, "认知工效学", "人因 负荷"
    )
    neighbors = store.neighbors(vector, limit=1)
    assert neighbors[0][0] == record.record_id


def test_exclude_removes_self(context) -> None:
    repository, store, embedder = context
    first, query = _store_record(repository, store, embedder, "人因工程", "负荷 评估")
    second, _ = _store_record(repository, store, embedder, "人因分析", "负荷 测量")
    neighbors = store.neighbors(query, limit=5, exclude=first.record_id)
    assert first.record_id not in [item[0] for item in neighbors]
    assert neighbors[0][0] == second.record_id


def test_no_json_vector_column_exists(context) -> None:
    _, store, _ = context
    columns = {
        row[1] for row in store.connection.execute("PRAGMA table_info(embeddings)")
    }
    assert "embedding" in columns
    assert "vector_json" not in columns


def test_dimension_mismatch_is_rejected(context) -> None:
    repository, store, _ = context
    record = repository.create(RecordDraft(title="t", body="b"))
    with pytest.raises(ValueError):
        store.put(
            record.record_id,
            [0.1, 0.2],
            model="m",
            dimensions=64,
            generation="g1",
            input_hash="h",
        )


def test_put_is_idempotent(context) -> None:
    repository, store, embedder = context
    record = repository.create(RecordDraft(title="t", body="b"))
    vector = embedder.embed(["t\nb"])[0]
    for _ in range(3):
        store.put(
            record.record_id,
            vector,
            model=embedder.model,
            dimensions=64,
            generation="g1",
            input_hash=record.content_hash,
        )
    assert store.count() == 1


def test_vector_survives_reconnect(tmp_path: Path) -> None:
    path = tmp_path / "engram.sqlite3"
    connection = connect(path)
    migrate(connection)
    repository = RecordRepository(connection)
    store = VectorStore(connection, dimensions=64)
    embedder = DeterministicEmbedder(dimensions=64)
    record = repository.create(RecordDraft(title="持久化", body="重连后仍可检索"))
    vector = embedder.embed(["持久化\n重连后仍可检索"])[0]
    store.put(
        record.record_id,
        vector,
        model=embedder.model,
        dimensions=64,
        generation="g1",
        input_hash=record.content_hash,
    )
    connection.close()

    reopened = connect(path)
    reopened_store = VectorStore(reopened, dimensions=64)
    assert reopened_store.count() == 1
    assert reopened_store.neighbors(vector, limit=1)[0][0] == record.record_id
