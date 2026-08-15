from pathlib import Path

import pytest

from engram.db import connect
from engram.domain import RecordDraft
from engram.migrations import migrate
from engram.repository import RecordRepository


@pytest.fixture()
def repository(tmp_path: Path) -> RecordRepository:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    counter = iter(f"rec_{index:04d}" for index in range(1, 1000))
    return RecordRepository(
        connection,
        id_factory=lambda: next(counter),
        clock=lambda: "2026-08-15T00:00:00Z",
    )


def test_create_enqueues_one_enrich_job(repository: RecordRepository) -> None:
    record = repository.create(RecordDraft(title="t", body="b"))
    jobs = repository.due_jobs("enrich", now="2026-08-15T00:00:00Z", limit=10)
    assert len(jobs) == 1
    assert jobs[0].record_id == record.record_id
    assert jobs[0].attempts == 0


def test_content_and_job_commit_together(tmp_path: Path) -> None:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    repository = RecordRepository(connection)
    repository.create(RecordDraft(title="t", body="b"))
    records = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    jobs = connection.execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0]
    assert records == jobs == 1


def test_future_jobs_are_not_due(repository: RecordRepository) -> None:
    record = repository.create(RecordDraft(title="t", body="b"))
    job = repository.due_jobs("enrich", now="2026-08-15T00:00:00Z", limit=10)[0]
    repository.reschedule_job(
        job.job_id,
        attempts=1,
        next_attempt_at="2026-08-15T02:00:00Z",
        error="ollama down",
        failure_kind="transient",
    )
    assert repository.due_jobs("enrich", now="2026-08-15T00:30:00Z", limit=10) == []
    later = repository.due_jobs("enrich", now="2026-08-15T03:00:00Z", limit=10)
    assert later[0].record_id == record.record_id
    assert later[0].attempts == 1


def test_completed_job_disappears(repository: RecordRepository) -> None:
    repository.create(RecordDraft(title="t", body="b"))
    job = repository.due_jobs("enrich", now="2026-08-15T00:00:00Z", limit=10)[0]
    repository.complete_job(job.job_id)
    assert repository.due_jobs("enrich", now="2099-01-01T00:00:00Z", limit=10) == []


def test_permanent_failure_stops_retrying(repository: RecordRepository) -> None:
    repository.create(RecordDraft(title="t", body="b"))
    job = repository.due_jobs("enrich", now="2026-08-15T00:00:00Z", limit=10)[0]
    repository.fail_job_permanently(job.job_id, error="model returned illegal label")
    assert repository.due_jobs("enrich", now="2099-01-01T00:00:00Z", limit=10) == []
    assert repository.backlog()["permanent"] == 1
