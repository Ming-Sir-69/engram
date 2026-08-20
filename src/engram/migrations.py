from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from engram.db import write_transaction
from engram.errors import SchemaIncompatibleError

SCHEMA_VERSION = 2

_MIGRATION_1 = """
CREATE TABLE records (
    record_id TEXT PRIMARY KEY,
    record_type TEXT NOT NULL
        CHECK (record_type IN ('note','reference','project')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','archived')),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    source_agent TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE record_projects (
    record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    PRIMARY KEY (record_id, project)
);

CREATE TABLE facets (
    record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('domain','tag')),
    value TEXT NOT NULL,
    provenance TEXT NOT NULL
        CHECK (provenance IN ('rule','knn','model','default','human')),
    confidence REAL NOT NULL DEFAULT 0.0,
    locked INTEGER NOT NULL DEFAULT 0,
    taxonomy_version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (record_id, kind, value)
);

CREATE TABLE record_links (
    source_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    relation TEXT NOT NULL DEFAULT 'related_to',
    score REAL NOT NULL DEFAULT 0.0,
    provenance TEXT NOT NULL DEFAULT 'knn',
    PRIMARY KEY (source_id, target_id, relation)
);

CREATE TABLE revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    summary TEXT NOT NULL
);

CREATE TABLE outbox_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    job_type TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    failure_kind TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (record_id, job_type)
);

CREATE TABLE embeddings (
    record_id TEXT PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    generation TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    embedding BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE records_fts USING fts5(
    record_id UNINDEXED,
    tokens,
    tokenize = 'unicode61'
);

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE INDEX idx_outbox_ready ON outbox_jobs(next_attempt_at);
CREATE INDEX idx_facets_value ON facets(kind, value);
"""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    from_version: int
    to_version: int
    applied: tuple[int, ...]


def _statements(script: str) -> tuple[str, ...]:
    return tuple(
        statement.strip() for statement in script.split(";") if statement.strip()
    )


def _apply_1(connection: sqlite3.Connection) -> None:
    # 必须逐条 execute：executescript() 会隐式提交当前事务，
    # 从而破坏调用方的 BEGIN IMMEDIATE 事务边界。
    for statement in _statements(_MIGRATION_1):
        connection.execute(statement)


_MIGRATION_2 = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _apply_2(connection: sqlite3.Connection) -> None:
    # curation 状态（last_curate_at / last_curate_count）放在库内而不是
    # sidecar：与 curate 执行同事务、随库备份，不需要额外的原子写代码。
    for statement in _statements(_MIGRATION_2):
        connection.execute(statement)


MIGRATIONS: Mapping[int, Callable[[sqlite3.Connection], None]] = {
    1: _apply_1,
    2: _apply_2,
}


def current_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    result = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return 0 if result[0] is None else int(result[0])


def migrate(
    connection: sqlite3.Connection, *, target: int | None = None
) -> MigrationResult:
    goal = SCHEMA_VERSION if target is None else target
    if goal > SCHEMA_VERSION:
        raise SchemaIncompatibleError(
            f"requested schema version {goal} exceeds supported {SCHEMA_VERSION}"
        )
    start = current_version(connection)
    applied: list[int] = []
    for version in range(start + 1, goal + 1):
        migration = MIGRATIONS[version]
        with write_transaction(connection) as tx:
            migration(tx)
            tx.execute(
                "INSERT INTO schema_migrations(version, applied_at) "
                "VALUES (?, datetime('now'))",
                (version,),
            )
        applied.append(version)
    return MigrationResult(from_version=start, to_version=goal, applied=tuple(applied))
