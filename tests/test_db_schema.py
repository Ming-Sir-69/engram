import sqlite3
from pathlib import Path

import pytest

from engram.db import connect, write_transaction
from engram.migrations import SCHEMA_VERSION, current_version, migrate


def test_fresh_database_has_all_tables(tmp_path: Path) -> None:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    assert {
        "records",
        "record_projects",
        "facets",
        "record_links",
        "revisions",
        "outbox_jobs",
        "embeddings",
        "records_fts",
        "schema_migrations",
    } <= names


def test_wal_and_foreign_keys_enabled(tmp_path: Path) -> None:
    connection = connect(tmp_path / "engram.sqlite3")
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migration_is_idempotent(tmp_path: Path) -> None:
    connection = connect(tmp_path / "engram.sqlite3")
    first = migrate(connection)
    second = migrate(connection)
    assert first.to_version == SCHEMA_VERSION
    assert second.applied == ()
    assert current_version(connection) == SCHEMA_VERSION


def test_record_type_is_constrained(tmp_path: Path) -> None:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO records(
                record_id, record_type, title, body, status,
                attributes_json, revision, created_at, updated_at,
                source_agent, content_hash
            ) VALUES ('r1','invalid','t','b','active','{}',1,'now','now','test','h')
            """
        )


def test_write_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    with pytest.raises(ValueError), write_transaction(connection) as tx:
        tx.execute(
            """
                INSERT INTO records(
                    record_id, record_type, title, body, status,
                    attributes_json, revision, created_at, updated_at,
                    source_agent, content_hash
                ) VALUES ('r1','note','t','b','active','{}',1,'now','now','test','h')
                """
        )
        raise ValueError("boom")
    assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
