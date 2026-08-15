from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from engram.db import write_transaction
from engram.domain import (
    RECORD_TYPES,
    Facet,
    Record,
    RecordDraft,
    content_hash_for,
)
from engram.errors import InvalidInputError, RecordNotFoundError
from engram.tokenizer import fts_document


def _default_id() -> str:
    return f"rec_{uuid.uuid4().hex[:20]}"


def _default_clock() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True, slots=True)
class OutboxJob:
    job_id: int
    record_id: str
    job_type: str
    attempts: int
    next_attempt_at: str
    last_error: str | None


class RecordRepository:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.connection = connection
        self._new_id = id_factory or _default_id
        self._now = clock or _default_clock

    def create(self, draft: RecordDraft) -> Record:
        title = draft.title.strip()
        body = draft.body.strip()
        if not title and not body:
            raise InvalidInputError("title and body cannot both be empty")
        if draft.record_type not in RECORD_TYPES:
            raise InvalidInputError(f"record_type must be one of {sorted(RECORD_TYPES)}")
        if not title:
            title = body.splitlines()[0][:80]
        digest = content_hash_for(title, body)
        existing = self.connection.execute(
            "SELECT record_id FROM records WHERE content_hash = ?", (digest,)
        ).fetchone()
        if existing is not None:
            return self.get(existing["record_id"])

        record_id = self._new_id()
        timestamp = self._now()
        with write_transaction(self.connection) as tx:
            tx.execute(
                """
                INSERT INTO records(
                    record_id, record_type, title, body, status,
                    attributes_json, revision, created_at, updated_at,
                    source_agent, content_hash
                ) VALUES (?, ?, ?, ?, 'active', ?, 1, ?, ?, ?, ?)
                """,
                (
                    record_id,
                    draft.record_type,
                    title,
                    body,
                    json.dumps(
                        dict(draft.attributes), ensure_ascii=False, sort_keys=True
                    ),
                    timestamp,
                    timestamp,
                    draft.source_agent,
                    digest,
                ),
            )
            for project in dict.fromkeys(draft.projects):
                tx.execute(
                    "INSERT INTO record_projects(record_id, project) VALUES (?, ?)",
                    (record_id, project),
                )
            tx.execute(
                "INSERT INTO records_fts(record_id, tokens) VALUES (?, ?)",
                (record_id, fts_document(f"{title}\n{body}")),
            )
            tx.execute(
                """
                INSERT INTO revisions(
                    record_id, revision, content_hash, changed_at,
                    changed_by, summary
                ) VALUES (?, 1, ?, ?, ?, 'created')
                """,
                (record_id, digest, timestamp, draft.source_agent),
            )
            tx.execute(
                """
                INSERT INTO outbox_jobs(
                    record_id, job_type, attempts, next_attempt_at, created_at
                ) VALUES (?, 'enrich', 0, ?, ?)
                """,
                (record_id, timestamp, timestamp),
            )
        return self.get(record_id)

    def get(self, record_id: str) -> Record:
        row = self.connection.execute(
            "SELECT * FROM records WHERE record_id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise RecordNotFoundError(record_id)
        return Record(
            record_id=row["record_id"],
            record_type=row["record_type"],
            title=row["title"],
            body=row["body"],
            status=row["status"],
            attributes=json.loads(row["attributes_json"]),
            projects=self.list_projects(record_id),
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            source_agent=row["source_agent"],
            content_hash=row["content_hash"],
            archived_at=row["archived_at"],
        )

    def list_projects(self, record_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT project FROM record_projects WHERE record_id = ? ORDER BY project",
            (record_id,),
        ).fetchall()
        return tuple(row["project"] for row in rows)

    def count(self) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])

    def due_jobs(
        self, job_type: str, *, now: str | None = None, limit: int = 20
    ) -> list[OutboxJob]:
        moment = now or self._now()
        rows = self.connection.execute(
            """
            SELECT * FROM outbox_jobs
            WHERE job_type = ?
              AND next_attempt_at <= ?
              AND (failure_kind IS NULL OR failure_kind != 'permanent')
            ORDER BY next_attempt_at, job_id
            LIMIT ?
            """,
            (job_type, moment, limit),
        ).fetchall()
        return [
            OutboxJob(
                job_id=row["job_id"],
                record_id=row["record_id"],
                job_type=row["job_type"],
                attempts=row["attempts"],
                next_attempt_at=row["next_attempt_at"],
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def complete_job(self, job_id: int) -> None:
        with write_transaction(self.connection) as tx:
            tx.execute("DELETE FROM outbox_jobs WHERE job_id = ?", (job_id,))

    def reschedule_job(
        self,
        job_id: int,
        *,
        attempts: int,
        next_attempt_at: str,
        error: str,
        failure_kind: str = "transient",
    ) -> None:
        with write_transaction(self.connection) as tx:
            tx.execute(
                """
                UPDATE outbox_jobs
                SET attempts = ?, next_attempt_at = ?,
                    last_error = ?, failure_kind = ?
                WHERE job_id = ?
                """,
                (attempts, next_attempt_at, error[:500], failure_kind, job_id),
            )

    def fail_job_permanently(self, job_id: int, *, error: str) -> None:
        with write_transaction(self.connection) as tx:
            tx.execute(
                """
                UPDATE outbox_jobs
                SET failure_kind = 'permanent', last_error = ?
                WHERE job_id = ?
                """,
                (error[:500], job_id),
            )

    def upsert_facets(self, facets: Iterable[Facet]) -> None:
        """写入标签。`locked` 的标签不被覆盖——人工判断优先于任何自动结果。"""
        with write_transaction(self.connection) as tx:
            for facet in facets:
                tx.execute(
                    """
                    INSERT INTO facets(
                        record_id, kind, value, provenance, confidence, locked
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(record_id, kind, value) DO UPDATE SET
                        provenance = excluded.provenance,
                        confidence = excluded.confidence
                    WHERE facets.locked = 0
                    """,
                    (
                        facet.record_id,
                        facet.kind,
                        facet.value,
                        facet.provenance,
                        facet.confidence,
                        int(facet.locked),
                    ),
                )

    def trusted_facets(self, record_id: str) -> tuple[Facet, ...]:
        """取该记录已有的可信标签（规则收割或人工确认）。"""
        rows = self.connection.execute(
            """
            SELECT kind, value, provenance, confidence, locked
            FROM facets
            WHERE record_id = ? AND (provenance IN ('rule','human') OR locked = 1)
            ORDER BY kind, value
            """,
            (record_id,),
        ).fetchall()
        return tuple(
            Facet(
                record_id=record_id,
                kind=row["kind"],
                value=row["value"],
                provenance=row["provenance"],
                confidence=row["confidence"],
                locked=bool(row["locked"]),
            )
            for row in rows
        )

    def backlog(self) -> dict[str, int]:
        row = self.connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN failure_kind = 'permanent' THEN 1 ELSE 0 END)
                    AS permanent
            FROM outbox_jobs
            """
        ).fetchone()
        return {
            "pending": int(row["total"] or 0),
            "permanent": int(row["permanent"] or 0),
        }
