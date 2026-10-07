"""Feedback inbox and privacy-preserving runtime observations.

The store is deliberately separate from the authoritative knowledge database.
Feedback is a human signal that needs triage; runtime events are disposable
measurements. Neither stores query bodies, record bodies, or tool results.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
EVENT_RETENTION_DAYS = 90
DEFAULT_SERVICE = "engram"
DEFAULT_MCP_OBSERVABILITY_PATH = (
    Path(os.environ.get("ENGRAM_DATA_DIR", Path.home() / "second-brain-data"))
    / "runtime"
    / "feedback.sqlite3"
)
_BUSY_TIMEOUT_MS = 100
_SCHEMA_LOCK = threading.Lock()
_FEEDBACK_STATUSES = frozenset(
    {"open", "investigating", "verified", "adopted", "rejected", "superseded"}
)
_CATEGORIES = frozenset(
    {"reliability", "quality", "ux", "performance", "security", "other"}
)
_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
_SECRET = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{16,}|bearer\s+[A-Za-z0-9._~+/=-]{12,}|"
    r"(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+)"
)
_SPACE = re.compile(r"\s+")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _clean(value: Any, limit: int) -> str:
    text = _SECRET.sub("[redacted]", str(value or ""))
    return _SPACE.sub(" ", text).strip()[:limit]


def _feedback_id() -> str:
    return "fb_" + uuid.uuid4().hex[:20]


def _event_id() -> str:
    return "evt_" + uuid.uuid4().hex[:20]


def validation_diagnostic(arguments: dict, error: Exception | None) -> dict:
    """Closed vocabulary only: never persist values or exception text."""
    if type(error).__name__ != "InvalidInputError":
        return {}
    field = getattr(error, "context", {}).get("argument")
    if field not in {"body", "query", "projects", "record_id"}:
        return {"code": "invalid_input", "field": "unknown", "input_type": "unknown"}
    value = arguments.get(field)
    kind = type(value).__name__ if type(value) in {str, int, float, bool, list, dict, type(None)} else "unknown"
    code = "missing" if field not in arguments else "invalid_type"
    if isinstance(value, str) and not value.strip():
        code = "empty_text"
    return {"code": code, "field": field, "input_type": kind}


def _safe_diagnostic(value) -> dict:
    if not isinstance(value, dict):
        return {}
    vocabulary = {
        "code": {"invalid_input", "missing", "invalid_type", "empty_text"},
        "field": {"body", "query", "projects", "record_id", "unknown"},
        "input_type": {"str", "int", "float", "bool", "list", "dict", "NoneType", "unknown"},
    }
    return {key: item for key, item in value.items() if key in vocabulary and isinstance(item, str) and item in vocabulary[key]}


class FeedbackStore:
    """A small local inbox whose failures never affect the main MCP call."""

    def __init__(self, path: Path, *, enabled: bool = True):
        self.path = Path(path)
        self.enabled = enabled

    def add(
        self,
        *,
        summary: str,
        category: str = "other",
        severity: str = "medium",
        expected: str = "",
        actual: str = "",
        tool: str = "",
        request_id: str = "",
        source: str = "mcp",
        service: str = DEFAULT_SERVICE,
        instance: str = "",
    ) -> dict[str, object]:
        summary = _clean(summary, 2000)
        if not summary:
            raise ValueError("summary is required")
        if category not in _CATEGORIES:
            raise ValueError("unknown feedback category")
        if severity not in _SEVERITIES:
            raise ValueError("unknown feedback severity")
        now = _now()
        item = {
            "feedback_id": _feedback_id(),
            "created_at": now,
            "updated_at": now,
            "status": "open",
            "category": category,
            "severity": severity,
            "summary": summary,
            "expected": _clean(expected, 1000),
            "actual": _clean(actual, 1000),
            "tool": _clean(tool, 80),
            "request_id": _clean(request_id, 120),
            "source": _clean(source, 80) or "mcp",
            "service": _clean(service, 80) or DEFAULT_SERVICE,
            "instance": _clean(instance, 120),
            "hypothesis": "",
            "resolution": "",
        }
        connection = self._open()
        if connection is None:
            raise OSError("feedback inbox unavailable")
        try:
            connection.execute(
                "INSERT INTO feedback (feedback_id,created_at,updated_at,status,"
                "category,severity,summary,expected,actual,tool,request_id,source,"
                "service,instance,hypothesis,resolution) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(item.values()),
            )
            connection.commit()
            return item
        finally:
            connection.close()

    def update(
        self,
        feedback_id: str,
        *,
        status: str,
        hypothesis: str = "",
        resolution: str = "",
    ) -> dict[str, object]:
        if status not in _FEEDBACK_STATUSES:
            raise ValueError("unknown feedback status")
        connection = self._open()
        if connection is None:
            raise OSError("feedback inbox unavailable")
        try:
            clean_id = _clean(feedback_id, 80)
            cursor = connection.execute(
                "UPDATE feedback SET status=?,updated_at=?,hypothesis=?,resolution=? "
                "WHERE feedback_id=?",
                (
                    status,
                    _now(),
                    _clean(hypothesis, 1000),
                    _clean(resolution, 2000),
                    clean_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError("feedback not found")
            connection.commit()
            row = connection.execute(
                "SELECT * FROM feedback WHERE feedback_id=?", (clean_id,)
            ).fetchone()
            return dict(row)
        finally:
            connection.close()

    def list(
        self,
        *,
        status: str | None = None,
        service: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        if status is not None and status not in _FEEDBACK_STATUSES:
            raise ValueError("unknown feedback status")
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        connection = self._open(readonly=True)
        if connection is None:
            return []
        try:
            query = "SELECT * FROM feedback"
            clauses: list[str] = []
            params: list[object] = []
            if status:
                clauses.append("status=?")
                params.append(status)
            if service and service != "all":
                clauses.append("service=?")
                params.append(_clean(service, 80))
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = connection.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def record_event(
        self,
        *,
        tool: str,
        variant: str,
        ok: bool,
        latency_ms: int,
        error_type: str = "",
        request_id: str = "",
        service: str = DEFAULT_SERVICE,
        instance: str = "",
        diagnostic: dict | None = None,
    ) -> bool:
        if (
            not self.enabled
            or not tool
            or not isinstance(latency_ms, int)
            or latency_ms < 0
        ):
            return False
        connection = self._open()
        if connection is None:
            return False
        cutoff = (
            datetime.now(UTC) - timedelta(days=EVENT_RETENTION_DAYS)
        ).isoformat()
        try:
            connection.execute(
                "INSERT INTO runtime_events(event_id,occurred_at,tool,variant,ok,"
                "latency_ms,error_type,request_id,service,instance,diagnostic) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _event_id(),
                    _now(),
                    _clean(tool, 80),
                    _clean(variant, 40) or "default",
                    int(ok),
                    latency_ms,
                    _clean(error_type, 120),
                    _clean(request_id, 120),
                    _clean(service, 80) or DEFAULT_SERVICE,
                    _clean(instance, 120),
                    json.dumps(_safe_diagnostic(diagnostic)),
                ),
            )
            connection.execute(
                "DELETE FROM runtime_events WHERE occurred_at < ?", (cutoff,)
            )
            connection.commit()
            return True
        except sqlite3.Error:
            connection.rollback()
            return False
        finally:
            connection.close()

    def snapshot(
        self,
        *,
        window_days: int = 14,
        limit: int = 10,
        service: str | None = None,
        now=None,
    ) -> dict[str, object]:
        if type(window_days) is not int or not 1 <= window_days <= 31:
            raise ValueError("window_days must be an integer from 1 to 31")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        end = now or datetime.now(UTC)
        start = end - timedelta(days=window_days)
        result = {
            "schema_version": SCHEMA_VERSION,
            "available": False,
            "read_only": True,
            "scope": _clean(service, 80) if service else "all-mcp",
            "window_days": window_days,
            "observed_at": end.astimezone(UTC).isoformat(timespec="seconds"),
            "events": {"calls": 0, "errors": 0, "error_rate": 0.0},
            "by_tool": [],
            "by_variant": [],
            "recent_errors": [],
            "feedback": {"open": 0, "total": 0, "items": []},
            "reinforcement": {"signals": [], "next_actions": []},
        }
        connection = self._open(readonly=True)
        if connection is None:
            result["reason"] = "feedback_store_missing_or_unreadable"
            return result
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runtime_events)")}
            diagnostic_column = "diagnostic" if "diagnostic" in columns else "'{}' AS diagnostic"
            query = (
                f"SELECT service,instance,tool,variant,ok,latency_ms,error_type,occurred_at,{diagnostic_column} "
                "FROM runtime_events WHERE occurred_at >= ? AND occurred_at < ?"
            )
            params: list[object] = [
                start.astimezone(UTC).isoformat(),
                end.astimezone(UTC).isoformat(),
            ]
            if service and service != "all":
                query += " AND service=?"
                params.append(_clean(service, 80))
            rows = connection.execute(
                query + " ORDER BY occurred_at DESC", params
            ).fetchall()
            by_tool: dict[str, dict[str, Any]] = defaultdict(
                lambda: {
                    "tool": "",
                    "calls": 0,
                    "errors": 0,
                    "total_latency_ms": 0,
                    "max_latency_ms": 0,
                }
            )
            by_variant: dict[str, dict[str, Any]] = defaultdict(
                lambda: {"tool": "", "variant": "", "calls": 0, "errors": 0}
            )
            by_service: dict[str, dict[str, Any]] = defaultdict(
                lambda: {"service": "", "calls": 0, "errors": 0}
            )
            recent_errors = []
            for row in rows:
                bucket = by_tool[row["tool"]]
                bucket["tool"] = row["tool"]
                bucket["calls"] += 1
                bucket["errors"] += int(not row["ok"])
                bucket["total_latency_ms"] += row["latency_ms"]
                bucket["max_latency_ms"] = max(
                    bucket["max_latency_ms"], row["latency_ms"]
                )
                variant_key = f"{row['tool']}:{row['variant']}"
                variant_bucket = by_variant[variant_key]
                variant_bucket["tool"] = row["tool"]
                variant_bucket["variant"] = row["variant"]
                variant_bucket["calls"] += 1
                variant_bucket["errors"] += int(not row["ok"])
                service_bucket = by_service[row["service"]]
                service_bucket["service"] = row["service"]
                service_bucket["calls"] += 1
                service_bucket["errors"] += int(not row["ok"])
                if not row["ok"] and len(recent_errors) < limit:
                    recent_errors.append(
                        {
                            "at": row["occurred_at"],
                            "tool": row["tool"],
                            "variant": row["variant"],
                            "error_type": row["error_type"],
                            "diagnostic": _safe_diagnostic(json.loads(row["diagnostic"])),
                        }
                    )
            calls = len(rows)
            errors = sum(int(not row["ok"]) for row in rows)
            tools = []
            for bucket in sorted(
                by_tool.values(), key=lambda item: (-item["calls"], item["tool"])
            ):
                calls_for_tool = bucket["calls"]
                tools.append(
                    {
                        "tool": bucket["tool"],
                        "calls": calls_for_tool,
                        "errors": bucket["errors"],
                        "error_rate": round(
                            bucket["errors"] / calls_for_tool, 4
                        ),
                        "avg_latency_ms": round(
                            bucket["total_latency_ms"] / calls_for_tool
                        ),
                        "max_latency_ms": bucket["max_latency_ms"],
                    }
                )
            variants = [
                {
                    "tool": bucket["tool"],
                    "variant": bucket["variant"],
                    "calls": bucket["calls"],
                    "errors": bucket["errors"],
                    "error_rate": round(bucket["errors"] / bucket["calls"], 4),
                }
                for bucket in sorted(
                    by_variant.values(),
                    key=lambda item: (-item["calls"], item["tool"], item["variant"]),
                )[:limit]
            ]
            services = [
                {
                    "service": bucket["service"],
                    "calls": bucket["calls"],
                    "errors": bucket["errors"],
                    "error_rate": round(bucket["errors"] / bucket["calls"], 4),
                }
                for bucket in sorted(
                    by_service.values(), key=lambda item: (-item["calls"], item["service"])
                )[:limit]
            ]
            feedback_query = "SELECT * FROM feedback"
            feedback_params: list[object] = []
            if service and service != "all":
                feedback_query += " WHERE service=?"
                feedback_params.append(_clean(service, 80))
            feedback_query += " ORDER BY created_at DESC LIMIT ?"
            feedback_params.append(limit)
            feedback_rows = connection.execute(
                feedback_query, feedback_params
            ).fetchall()
            open_query = (
                "SELECT COUNT(*) FROM feedback "
                "WHERE status IN ('open','investigating')"
            )
            open_params: list[object] = []
            if service and service != "all":
                open_query += " AND service=?"
                open_params.append(_clean(service, 80))
            open_count = connection.execute(
                open_query, open_params
            ).fetchone()[0]
            result.update(
                available=True,
                events={
                    "calls": calls,
                    "errors": errors,
                    "error_rate": round(errors / calls, 4) if calls else 0.0,
                },
                by_tool=tools[:limit],
                by_variant=variants,
                by_service=services,
                recent_errors=recent_errors,
                feedback={
                    "open": open_count,
                    "total": connection.execute(
                        "SELECT COUNT(*) FROM feedback"
                    ).fetchone()[0],
                    "items": [dict(row) for row in feedback_rows],
                },
            )
        finally:
            connection.close()
        result["reinforcement"] = _reinforcement(result)
        return result

    def evidence(
        self, *, window_days: int = 14, service: str | None = DEFAULT_SERVICE
    ) -> dict[str, object]:
        """Stable, content-free evidence for the source self-improvement runner."""
        snapshot = self.snapshot(
            window_days=window_days, limit=10, service=service
        )
        snapshot.pop("observed_at", None)
        return snapshot

    def _open(self, *, readonly: bool = False) -> sqlite3.Connection | None:
        if not self.enabled:
            return None
        connection: sqlite3.Connection | None = None
        try:
            if readonly:
                if not self.path.is_file():
                    return None
                connection = sqlite3.connect(
                    self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
                )
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(self.path.parent, 0o700)
                connection = sqlite3.connect(
                    self.path, timeout=_BUSY_TIMEOUT_MS / 1000
                )
                with _SCHEMA_LOCK:
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS feedback ("
                        "feedback_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
                        "updated_at TEXT NOT NULL, status TEXT NOT NULL, "
                        "category TEXT NOT NULL, severity TEXT NOT NULL, "
                        "summary TEXT NOT NULL, expected TEXT NOT NULL, actual TEXT NOT NULL, "
                        "tool TEXT NOT NULL, request_id TEXT NOT NULL, source TEXT NOT NULL, "
                        "service TEXT NOT NULL DEFAULT 'engram', "
                        "instance TEXT NOT NULL DEFAULT '', "
                        "hypothesis TEXT NOT NULL, resolution TEXT NOT NULL)"
                    )
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS runtime_events ("
                        "event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, "
                        "tool TEXT NOT NULL, variant TEXT NOT NULL, ok INTEGER NOT NULL, "
                        "latency_ms INTEGER NOT NULL, error_type TEXT NOT NULL, "
                        "request_id TEXT NOT NULL, service TEXT NOT NULL DEFAULT 'engram', "
                        "instance TEXT NOT NULL DEFAULT '')"
                    )
                    self._migrate_columns(connection)
                    connection.execute(
                        "CREATE INDEX IF NOT EXISTS idx_events_at "
                        "ON runtime_events(occurred_at)"
                    )
                    connection.commit()
            connection.row_factory = sqlite3.Row
            if not readonly:
                os.chmod(self.path, 0o600)
            return connection
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            return None

    @staticmethod
    def _migrate_columns(connection: sqlite3.Connection) -> None:
        """Add generic MCP ownership fields to the original Engram sidecar."""

        tables = {
            "feedback": {
                "service": "TEXT NOT NULL DEFAULT 'engram'",
                "instance": "TEXT NOT NULL DEFAULT ''",
            },
            "runtime_events": {
                "service": "TEXT NOT NULL DEFAULT 'engram'",
                "instance": "TEXT NOT NULL DEFAULT ''",
                "diagnostic": "TEXT NOT NULL DEFAULT '{}'",
            },
        }
        for table, columns in tables.items():
            present = {
                row[1]
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for name, definition in columns.items():
                if name not in present:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                    )


class MCPObservability:
    """Small adapter for any MCP server, independent of Engram RSM."""

    def __init__(
        self,
        service: str,
        *,
        instance: str = "",
        path: Path | None = None,
        enabled: bool = True,
    ):
        self.service = _clean(service, 80) or "unknown-mcp"
        self.instance = _clean(instance, 120)
        self.store = FeedbackStore(
            path or DEFAULT_MCP_OBSERVABILITY_PATH, enabled=enabled
        )

    def record_event(
        self,
        *,
        tool: str,
        variant: str = "default",
        ok: bool,
        latency_ms: int,
        error_type: str = "",
        request_id: str = "",
    ) -> bool:
        return self.store.record_event(
            service=self.service,
            instance=self.instance,
            tool=tool,
            variant=variant,
            ok=ok,
            latency_ms=latency_ms,
            error_type=error_type,
            request_id=request_id,
        )

    def submit_feedback(self, **fields: Any) -> dict[str, object]:
        fields.update(service=self.service, instance=self.instance)
        return self.store.add(**fields)

    def report(self, *, window_days: int = 14, limit: int = 10) -> dict[str, object]:
        return self.store.snapshot(
            window_days=window_days,
            limit=limit,
            service=self.service,
        )


def _reinforcement(snapshot: dict[str, object]) -> dict[str, list[dict[str, object]]]:
    signals: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []
    feedback = snapshot["feedback"]
    if feedback["open"]:
        signals.append({"code": "open_feedback", "count": feedback["open"]})
        actions.append(
            {
                "priority": "high",
                "action": "triage_open_feedback",
                "count": feedback["open"],
            }
        )
    for row in snapshot["by_tool"]:
        if row["errors"] >= 3 or (
            row["calls"] >= 5 and row["error_rate"] >= 0.1
        ):
            signals.append(
                {
                    "code": "repeated_tool_errors",
                    "tool": row["tool"],
                    "errors": row["errors"],
                }
            )
            actions.append(
                {
                    "priority": "high",
                    "action": "investigate_tool",
                    "tool": row["tool"],
                }
            )
        if row["calls"] >= 5 and row["avg_latency_ms"] >= 1000:
            signals.append(
                {
                    "code": "slow_tool",
                    "tool": row["tool"],
                    "avg_latency_ms": row["avg_latency_ms"],
                }
            )
            actions.append(
                {"priority": "medium", "action": "profile_tool", "tool": row["tool"]}
            )
    calls = snapshot["events"]["calls"]
    if calls and snapshot["by_tool"] and snapshot["by_tool"][0]["calls"] / calls >= 0.5:
        signals.append(
            {
                "code": "high_use_operation",
                "tool": snapshot["by_tool"][0]["tool"],
                "calls": snapshot["by_tool"][0]["calls"],
            }
        )
        actions.append(
            {
                "priority": "low",
                "action": "review_hot_path",
                "tool": snapshot["by_tool"][0]["tool"],
            }
        )
    return {"signals": signals, "next_actions": actions}


def feedback_statuses() -> list[str]:
    return sorted(_FEEDBACK_STATUSES)


def feedback_categories() -> list[str]:
    return sorted(_CATEGORIES)


def feedback_severities() -> list[str]:
    return sorted(_SEVERITIES)
