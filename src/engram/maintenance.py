from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
RETENTION_DAYS = 90
_BUSY_TIMEOUT_MS = 50
_ALLOWED_TOOLS = frozenset({"remember", "recall", "get", "status"})
_ALLOWED_VARIANTS = frozenset({"default", "keyword", "vector", "hybrid", "invalid"})
_STATES = frozenset({"healthy", "warning"})

_USAGE_TABLE = """
CREATE TABLE IF NOT EXISTS usage_daily (
    day TEXT NOT NULL,
    tool TEXT NOT NULL
        CHECK (tool IN ('remember','recall','get','status')),
    variant TEXT NOT NULL
        CHECK (variant IN ('default','keyword','vector','hybrid','invalid')),
    calls INTEGER NOT NULL DEFAULT 0 CHECK (calls >= 0),
    errors INTEGER NOT NULL DEFAULT 0
        CHECK (errors >= 0 AND errors <= calls),
    total_latency_ms INTEGER NOT NULL DEFAULT 0
        CHECK (total_latency_ms >= 0),
    max_latency_ms INTEGER NOT NULL DEFAULT 0
        CHECK (max_latency_ms >= 0),
    PRIMARY KEY (day, tool, variant)
) WITHOUT ROWID
"""

_MAINTENANCE_TABLE = """
CREATE TABLE IF NOT EXISTS maintenance_daily (
    day TEXT PRIMARY KEY,
    state TEXT NOT NULL
        CHECK (state IN ('healthy','warning')),
    elapsed_ms INTEGER NOT NULL DEFAULT 0 CHECK (elapsed_ms >= 0),
    records INTEGER CHECK (records IS NULL OR records >= 0),
    backlog_pending INTEGER
        CHECK (backlog_pending IS NULL OR backlog_pending >= 0),
    backlog_permanent INTEGER
        CHECK (backlog_permanent IS NULL OR backlog_permanent >= 0),
    vectors INTEGER CHECK (vectors IS NULL OR vectors >= 0),
    curation_due INTEGER
        CHECK (curation_due IS NULL OR curation_due IN (0, 1)),
    stage2_ready INTEGER
        CHECK (stage2_ready IS NULL OR stage2_ready IN (0, 1))
) WITHOUT ROWID
"""


@dataclass(frozen=True, slots=True)
class UsageSidecar:
    """可删除的本地日聚合；任何失败都由调用方视为一次丢样。"""

    path: Path
    enabled: bool = True

    def record_and_check(
        self,
        *,
        day: str,
        tool: str,
        variant: str,
        error: bool,
        latency_ms: int,
        check_daily: bool,
    ) -> bool:
        if (
            not self.enabled
            or tool not in _ALLOWED_TOOLS
            or variant not in _ALLOWED_VARIANTS
            or not _valid_day(day)
            or not isinstance(latency_ms, int)
            or latency_ms < 0
        ):
            return False
        connection = self._open()
        if connection is None:
            return False
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO usage_daily("
                "day, tool, variant, calls, errors, total_latency_ms, max_latency_ms"
                ") VALUES (?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(day, tool, variant) DO UPDATE SET "
                "calls = calls + 1, "
                "errors = errors + excluded.errors, "
                "total_latency_ms = total_latency_ms + excluded.total_latency_ms, "
                "max_latency_ms = MAX(max_latency_ms, excluded.max_latency_ms)",
                (day, tool, variant, int(error), latency_ms, latency_ms),
            )
            daily_due = False
            if check_daily:
                daily_due = (
                    connection.execute(
                        "SELECT 1 FROM maintenance_daily WHERE day = ?", (day,)
                    ).fetchone()
                    is None
                )
            connection.execute("COMMIT")
            return daily_due
        except Exception:  # noqa: BLE001 - sidecar 必须 never-raise
            _rollback(connection)
            return False
        finally:
            connection.close()

    def finish_daily(
        self,
        *,
        day: str,
        state: str,
        elapsed_ms: int,
        records: int,
        backlog_pending: int,
        backlog_permanent: int,
        vectors: int,
        curation_due: bool,
        stage2_ready: bool,
    ) -> bool:
        values = (
            elapsed_ms,
            records,
            backlog_pending,
            backlog_permanent,
            vectors,
        )
        if (
            not self.enabled
            or state not in _STATES
            or not _valid_day(day)
            or any(not isinstance(value, int) or value < 0 for value in values)
        ):
            return False
        connection = self._open()
        if connection is None:
            return False
        cutoff = (date.fromisoformat(day) - timedelta(days=RETENTION_DAYS)).isoformat()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT OR IGNORE INTO maintenance_daily("
                "day, state, elapsed_ms, records, backlog_pending, "
                "backlog_permanent, vectors, curation_due, stage2_ready"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    day,
                    state,
                    elapsed_ms,
                    records,
                    backlog_pending,
                    backlog_permanent,
                    vectors,
                    int(curation_due),
                    int(stage2_ready),
                ),
            )
            connection.execute("DELETE FROM usage_daily WHERE day < ?", (cutoff,))
            connection.execute("DELETE FROM maintenance_daily WHERE day < ?", (cutoff,))
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:  # noqa: BLE001 - sidecar 必须 never-raise
            _rollback(connection)
            return False
        finally:
            connection.close()

    def _open(self) -> sqlite3.Connection | None:
        if not self.enabled:
            return None
        connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
            connection = sqlite3.connect(
                self.path,
                timeout=_BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA synchronous=NORMAL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                connection.close()
                return None
            if version == 0:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute("PRAGMA user_version").fetchone()[0]
                if current == 0:
                    connection.execute(_USAGE_TABLE)
                    connection.execute(_MAINTENANCE_TABLE)
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                elif current > SCHEMA_VERSION:
                    connection.execute("ROLLBACK")
                    connection.close()
                    return None
                connection.execute("COMMIT")
            os.chmod(self.path, 0o600)
            return connection
        except Exception:  # noqa: BLE001 - sidecar 必须 never-raise
            if connection is not None:
                _rollback(connection)
                connection.close()
            return None


def build_maintenance_warning(
    status: dict[str, Any],
) -> dict[str, list[dict[str, object]]] | None:
    """只从权威状态中提取低歧义异常，不做启发式健康评分。"""

    records = int(status["records"])
    vectors = int(status["vectors"])
    backlog = status["backlog"]
    curation = status["curation_due"]
    pending = int(backlog["pending"])
    permanent = int(backlog["permanent"])
    issues: list[dict[str, object]] = []
    if permanent > 0:
        issues.append({"code": "permanent_backlog", "count": permanent})
    if pending == 0 and vectors < records:
        issues.append(
            {
                "code": "missing_vectors_without_backlog",
                "missing": records - vectors,
            }
        )
    if curation["due"] and curation["reason"] in {
        "never_curated",
        "new_records",
    }:
        issues.append(
            {
                "code": "curation_due",
                "reason": curation["reason"],
                "new_records": int(curation["new_records"]),
            }
        )
    return {"issues": issues} if issues else None


def _valid_day(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _rollback(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        return
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass
