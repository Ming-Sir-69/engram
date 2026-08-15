from pathlib import Path

import pytest

from engram.db import connect
from engram.domain import RecordDraft
from engram.errors import InvalidInputError, RecordNotFoundError
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


def test_create_assigns_stable_id_and_revision_one(
    repository: RecordRepository,
) -> None:
    record = repository.create(RecordDraft(title="灵感", body="正文"))
    assert record.record_id == "rec_0001"
    assert record.revision == 1
    assert record.status == "active"
    assert record.record_type == "note"


def test_same_content_is_idempotent(repository: RecordRepository) -> None:
    first = repository.create(RecordDraft(title="灵感", body="正文"))
    second = repository.create(RecordDraft(title="灵感", body="正文"))
    assert first.record_id == second.record_id
    assert repository.count() == 1


def test_projects_are_stored_and_returned(repository: RecordRepository) -> None:
    record = repository.create(
        RecordDraft(title="t", body="b", projects=("engram", "jarvis-lite"))
    )
    assert repository.get(record.record_id).projects == ("engram", "jarvis-lite")


def test_invalid_record_type_is_rejected(repository: RecordRepository) -> None:
    with pytest.raises(InvalidInputError):
        repository.create(RecordDraft(title="t", body="b", record_type="idea"))


def test_empty_title_and_body_is_rejected(repository: RecordRepository) -> None:
    with pytest.raises(InvalidInputError):
        repository.create(RecordDraft(title="   ", body="  "))


def test_get_missing_record_raises(repository: RecordRepository) -> None:
    with pytest.raises(RecordNotFoundError):
        repository.get("rec_absent")


def test_create_writes_one_revision(repository: RecordRepository) -> None:
    record = repository.create(RecordDraft(title="t", body="b"))
    rows = repository.connection.execute(
        "SELECT revision, changed_by FROM revisions WHERE record_id = ?",
        (record.record_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["revision"] == 1


def test_title_defaults_to_first_body_line(repository: RecordRepository) -> None:
    record = repository.create(RecordDraft(title="", body="第一行\n第二行"))
    assert record.title == "第一行"
