from pathlib import Path

import pytest

from engram.classify import Classifier
from engram.db import connect
from engram.domain import RecordDraft
from engram.embedding import DeterministicEmbedder
from engram.enrich import BACKOFF_SECONDS, EnrichmentService
from engram.errors import ModelUnavailableError
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.vectors import VectorStore


class BrokenEmbedder:
    """模拟 Ollama 未运行：暂时性失败，应当退避重试。"""

    model = "broken"
    dimensions = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise ModelUnavailableError("ollama down")


class BadDimensionEmbedder:
    """模拟响应维度不符：确定性失败，重试没有意义。"""

    model = "bad"
    dimensions = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise ValueError("embedding dimensions mismatch")


@pytest.fixture()
def context(tmp_path: Path):
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    counter = iter(f"rec_{index:04d}" for index in range(1, 100))
    repository = RecordRepository(
        connection,
        id_factory=lambda: next(counter),
        clock=lambda: "2026-08-15T00:00:00Z",
    )
    store = VectorStore(connection, dimensions=8)
    return repository, store


def build_service(repository, store, embedder) -> EnrichmentService:
    return EnrichmentService(
        repository=repository,
        store=store,
        embedder=embedder,
        classifier=Classifier(store=store, model=None),
        generation="g1",
    )


def test_drain_embeds_classifies_and_clears_job(context) -> None:
    repository, store = context
    record = repository.create(RecordDraft(title="认知工效学", body="人因 负荷"))
    service = build_service(repository, store, DeterministicEmbedder(dimensions=8))
    result = service.drain(now="2026-08-15T00:00:00Z")
    assert result.succeeded == 1
    assert store.count() == 1
    assert repository.backlog()["pending"] == 0
    facets = repository.connection.execute(
        "SELECT COUNT(*) FROM facets WHERE record_id = ?", (record.record_id,)
    ).fetchone()[0]
    assert facets >= 1


def test_transient_failure_uses_exponential_backoff(context) -> None:
    repository, store = context
    repository.create(RecordDraft(title="t", body="b"))
    service = build_service(repository, store, BrokenEmbedder())
    result = service.drain(now="2026-08-15T00:00:00Z")
    assert result.failed_transient == 1
    job = repository.connection.execute("SELECT * FROM outbox_jobs").fetchone()
    assert job["attempts"] == 1
    assert job["failure_kind"] == "transient"
    assert job["next_attempt_at"] > "2026-08-15T00:00:00Z"
    assert BACKOFF_SECONDS[0] == 60


def test_backoff_grows_with_attempts(context) -> None:
    repository, store = context
    repository.create(RecordDraft(title="t", body="b"))
    service = build_service(repository, store, BrokenEmbedder())
    service.drain(now="2026-08-15T00:00:00Z")
    first = repository.connection.execute(
        "SELECT next_attempt_at FROM outbox_jobs"
    ).fetchone()[0]
    service.drain(now="2026-08-15T01:00:00Z")
    second_row = repository.connection.execute(
        "SELECT attempts, next_attempt_at FROM outbox_jobs"
    ).fetchone()
    assert second_row["attempts"] == 2
    assert first == "2026-08-15T00:01:00Z"
    assert second_row["next_attempt_at"] == "2026-08-15T01:05:00Z"


def test_permanent_failure_is_not_retried(context) -> None:
    repository, store = context
    repository.create(RecordDraft(title="t", body="b"))
    service = build_service(repository, store, BadDimensionEmbedder())
    result = service.drain(now="2026-08-15T00:00:00Z")
    assert result.failed_permanent == 1
    job = repository.connection.execute("SELECT * FROM outbox_jobs").fetchone()
    assert job["failure_kind"] == "permanent"
    assert repository.due_jobs("enrich", now="2099-01-01T00:00:00Z") == []


def test_drain_is_idempotent(context) -> None:
    repository, store = context
    repository.create(RecordDraft(title="t", body="b"))
    service = build_service(repository, store, DeterministicEmbedder(dimensions=8))
    first = service.drain(now="2026-08-15T00:00:00Z")
    second = service.drain(now="2026-08-15T00:00:00Z")
    assert first.succeeded == 1
    assert second.processed == 0


def test_content_survives_enrichment_failure(context) -> None:
    repository, store = context
    record = repository.create(RecordDraft(title="重要", body="不能丢"))
    service = build_service(repository, store, BrokenEmbedder())
    service.drain(now="2026-08-15T00:00:00Z")
    assert repository.get(record.record_id).body == "不能丢"


def test_locked_facet_is_not_overwritten(context) -> None:
    repository, store = context
    record = repository.create(RecordDraft(title="认知工效学", body="人因"))
    repository.connection.execute(
        "INSERT INTO facets(record_id, kind, value, provenance, confidence, locked) "
        "VALUES (?, 'domain', 'unsorted', 'human', 1.0, 1)",
        (record.record_id,),
    )
    service = build_service(repository, store, DeterministicEmbedder(dimensions=8))
    service.drain(now="2026-08-15T00:00:00Z")
    row = repository.connection.execute(
        "SELECT provenance, confidence FROM facets "
        "WHERE record_id = ? AND kind = 'domain' AND value = 'unsorted'",
        (record.record_id,),
    ).fetchone()
    assert row["provenance"] == "human"
    assert row["confidence"] == 1.0


def test_links_are_created_between_similar_records(context) -> None:
    repository, store = context
    embedder = DeterministicEmbedder(dimensions=8)
    service = build_service(repository, store, embedder)
    repository.create(RecordDraft(title="人因工程", body="负荷 评估 标准"))
    service.drain(now="2026-08-15T00:00:00Z")
    repository.create(RecordDraft(title="人因分析", body="负荷 评估 测量"))
    service.drain(now="2026-08-15T00:10:00Z")
    total = repository.connection.execute(
        "SELECT COUNT(*) FROM record_links"
    ).fetchone()[0]
    assert total >= 2
