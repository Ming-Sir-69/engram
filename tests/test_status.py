"""status 触发器：curation_due 与 stage2_ready。

这两个标志是状态机而不是提醒：每次 status（CLI 或 MCP）都重新计算，
任何接入的 Agent 都能看到，不依赖谁记得去查日历。
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from engram.db import connect
from engram.domain import RecordDraft
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.status import collect_status

NOW = datetime(2026, 8, 20, tzinfo=UTC)


@pytest.fixture()
def context(tmp_path: Path):
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    counter = iter(f"rec_{index:04d}" for index in range(1, 200))
    repository = RecordRepository(connection, id_factory=lambda: next(counter))
    return repository, tmp_path


def _stamp_curation(repository: RecordRepository, *, at: datetime, count: int) -> None:
    repository.connection.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        [
            ("last_curate_at", at.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("last_curate_count", str(count)),
        ],
    )


def test_fresh_database_reports_curation_due(context) -> None:
    repository, data_dir = context
    payload = collect_status(repository=repository, data_dir=data_dir, now=NOW)
    assert payload["curation_due"]["due"] is True
    assert payload["curation_due"]["reason"] == "never_curated"


def test_recent_curation_is_not_due(context) -> None:
    repository, data_dir = context
    repository.create(RecordDraft(title="t", body="b"))
    _stamp_curation(repository, at=NOW - timedelta(days=1), count=1)
    payload = collect_status(repository=repository, data_dir=data_dir, now=NOW)
    assert payload["curation_due"]["due"] is False


def test_twenty_new_records_trigger_curation(context) -> None:
    repository, data_dir = context
    _stamp_curation(repository, at=NOW - timedelta(days=1), count=0)
    for index in range(20):
        repository.create(RecordDraft(title=f"t{index}", body="b"))
    payload = collect_status(repository=repository, data_dir=data_dir, now=NOW)
    due = payload["curation_due"]
    assert due["due"] is True
    assert due["reason"] == "new_records"
    assert due["new_records"] == 20


def test_stale_curation_triggers_even_without_new_records(context) -> None:
    repository, data_dir = context
    _stamp_curation(repository, at=NOW - timedelta(days=8), count=0)
    payload = collect_status(repository=repository, data_dir=data_dir, now=NOW)
    due = payload["curation_due"]
    assert due["due"] is True
    assert due["reason"] == "age"


def test_stage2_blocked_by_record_count(context) -> None:
    repository, data_dir = context
    repository.create(RecordDraft(title="t", body="b"))
    (data_dir / "eval-anchors.jsonl").write_text("{}\n", encoding="utf-8")
    payload = collect_status(repository=repository, data_dir=data_dir, now=NOW)
    stage2 = payload["stage2_ready"]
    assert stage2["ready"] is False
    assert stage2["anchors_present"] is True


def test_stage2_blocked_by_missing_anchors(context, monkeypatch) -> None:
    repository, data_dir = context
    monkeypatch.setattr("engram.status.STAGE2_MIN_RECORDS", 2)
    for index in range(2):
        repository.create(RecordDraft(title=f"t{index}", body="b"))
    payload = collect_status(repository=repository, data_dir=data_dir, now=NOW)
    stage2 = payload["stage2_ready"]
    assert stage2["ready"] is False
    assert stage2["anchors_present"] is False
    # 输出必须带检查路径：放错位置时用户能看到为什么是 false
    assert stage2["anchors_path"] == str(data_dir / "eval-anchors.jsonl")


def test_stage2_ready_when_both_conditions_met(context, monkeypatch) -> None:
    repository, data_dir = context
    monkeypatch.setattr("engram.status.STAGE2_MIN_RECORDS", 2)
    for index in range(2):
        repository.create(RecordDraft(title=f"t{index}", body="b"))
    (data_dir / "eval-anchors.jsonl").write_text("{}\n", encoding="utf-8")
    payload = collect_status(repository=repository, data_dir=data_dir, now=NOW)
    assert payload["stage2_ready"]["ready"] is True


def test_status_keeps_existing_fields(context) -> None:
    repository, data_dir = context
    repository.create(RecordDraft(title="t", body="b"))
    payload = collect_status(repository=repository, data_dir=data_dir, now=NOW)
    assert payload["records"] == 1
    assert payload["vectors"] == 0
    assert payload["data_dir"] == str(data_dir)
    assert "pending" in payload["backlog"]
