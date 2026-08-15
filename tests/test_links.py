from pathlib import Path

import pytest

from engram.db import connect
from engram.domain import RecordDraft
from engram.links import build_links
from engram.migrations import migrate
from engram.repository import RecordRepository


@pytest.fixture()
def repository(tmp_path: Path) -> RecordRepository:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    counter = iter(f"rec_{index:04d}" for index in range(1, 100))
    return RecordRepository(connection, id_factory=lambda: next(counter))


def test_links_are_written_both_ways(repository: RecordRepository) -> None:
    first = repository.create(RecordDraft(title="a", body="a"))
    second = repository.create(RecordDraft(title="b", body="b"))
    written = build_links(
        repository.connection, first.record_id, [(second.record_id, 0.9)]
    )
    assert written == 2
    rows = repository.connection.execute(
        "SELECT source_id, target_id FROM record_links ORDER BY source_id"
    ).fetchall()
    assert (rows[0]["source_id"], rows[0]["target_id"]) == (
        first.record_id,
        second.record_id,
    )
    assert (rows[1]["source_id"], rows[1]["target_id"]) == (
        second.record_id,
        first.record_id,
    )


def test_links_are_idempotent(repository: RecordRepository) -> None:
    first = repository.create(RecordDraft(title="a", body="a"))
    second = repository.create(RecordDraft(title="b", body="b"))
    for _ in range(3):
        build_links(repository.connection, first.record_id, [(second.record_id, 0.9)])
    total = repository.connection.execute(
        "SELECT COUNT(*) FROM record_links"
    ).fetchone()[0]
    assert total == 2


def test_self_link_is_ignored(repository: RecordRepository) -> None:
    record = repository.create(RecordDraft(title="a", body="a"))
    assert (
        build_links(repository.connection, record.record_id, [(record.record_id, 1.0)])
        == 0
    )


def test_empty_neighbors_write_nothing(repository: RecordRepository) -> None:
    record = repository.create(RecordDraft(title="a", body="a"))
    assert build_links(repository.connection, record.record_id, []) == 0
