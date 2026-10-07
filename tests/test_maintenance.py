from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from engram.maintenance import UsageSidecar, build_maintenance_warning

DAY = "2026-08-21"


def _rows(path: Path, query: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query).fetchall()
    finally:
        connection.close()


def test_first_use_creates_private_v1_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "usage.sqlite3"
    sidecar = UsageSidecar(path=path, enabled=True)

    assert (
        sidecar.record_and_check(
            day=DAY,
            tool="recall",
            variant="hybrid",
            error=False,
            latency_ms=12,
            check_daily=True,
        )
        is True
    )

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert tables == {"usage_daily", "maintenance_daily"}
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_usage_is_aggregated_and_daily_completion_is_unique(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "usage.sqlite3"
    first = UsageSidecar(path=path, enabled=True)
    second = UsageSidecar(path=path, enabled=True)

    assert (
        first.record_and_check(
            day=DAY,
            tool="recall",
            variant="keyword",
            error=False,
            latency_ms=10,
            check_daily=True,
        )
        is True
    )
    assert (
        first.finish_daily(
            day=DAY,
            state="healthy",
            elapsed_ms=1,
            records=0,
            backlog_pending=0,
            backlog_permanent=0,
            vectors=0,
            curation_due=False,
            stage2_ready=False,
        )
        is True
    )
    assert (
        second.record_and_check(
            day=DAY,
            tool="recall",
            variant="keyword",
            error=True,
            latency_ms=25,
            check_daily=True,
        )
        is False
    )

    row = _rows(path, "SELECT * FROM usage_daily")[0]
    assert dict(row) == {
        "day": DAY,
        "tool": "recall",
        "variant": "keyword",
        "calls": 2,
        "errors": 1,
        "total_latency_ms": 35,
        "max_latency_ms": 25,
    }
    maintenance = _rows(path, "SELECT day, state FROM maintenance_daily")
    assert [tuple(row) for row in maintenance] == [(DAY, "healthy")]


def test_concurrent_daily_completion_has_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "usage.sqlite3"
    sidecars = [UsageSidecar(path=path, enabled=True) for _ in range(4)]

    def complete(sidecar: UsageSidecar) -> bool:
        due = sidecar.record_and_check(
            day=DAY,
            tool="status",
            variant="default",
            error=False,
            latency_ms=1,
            check_daily=True,
        )
        if not due:
            return False
        return sidecar.finish_daily(
            day=DAY,
            state="healthy",
            elapsed_ms=1,
            records=0,
            backlog_pending=0,
            backlog_permanent=0,
            vectors=0,
            curation_due=False,
            stage2_ready=False,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(complete, sidecars))

    assert results.count(True) == 1
    assert sum(row["calls"] for row in _rows(path, "SELECT * FROM usage_daily")) >= 1


def test_finish_daily_records_snapshot_and_prunes_old_days(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "usage.sqlite3"
    sidecar = UsageSidecar(path=path, enabled=True)
    for day in ("2026-05-22", "2026-05-23"):
        sidecar.record_and_check(
            day=day,
            tool="status",
            variant="default",
            error=False,
            latency_ms=1,
            check_daily=True,
        )
        assert sidecar.finish_daily(
            day=day,
            state="healthy",
            elapsed_ms=1,
            records=1,
            backlog_pending=0,
            backlog_permanent=0,
            vectors=1,
            curation_due=False,
            stage2_ready=False,
        )
    sidecar.record_and_check(
        day=DAY,
        tool="status",
        variant="default",
        error=False,
        latency_ms=1,
        check_daily=True,
    )

    assert (
        sidecar.finish_daily(
            day=DAY,
            state="healthy",
            elapsed_ms=4,
            records=154,
            backlog_pending=0,
            backlog_permanent=0,
            vectors=154,
            curation_due=False,
            stage2_ready=False,
        )
        is True
    )

    usage_days = {row["day"] for row in _rows(path, "SELECT day FROM usage_daily")}
    maintenance_days = {
        row["day"] for row in _rows(path, "SELECT day FROM maintenance_daily")
    }
    assert "2026-05-22" not in usage_days
    assert "2026-05-22" not in maintenance_days
    assert "2026-05-23" in usage_days
    snapshot = _rows(path, f"SELECT * FROM maintenance_daily WHERE day = '{DAY}'")[0]
    assert snapshot["state"] == "healthy"
    assert snapshot["records"] == 154
    assert snapshot["vectors"] == 154


def test_disabled_sidecar_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "usage.sqlite3"
    sidecar = UsageSidecar(path=path, enabled=False)

    assert (
        sidecar.record_and_check(
            day=DAY,
            tool="get",
            variant="default",
            error=False,
            latency_ms=1,
            check_daily=True,
        )
        is False
    )
    assert not path.parent.exists()


def test_invalid_projection_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "usage.sqlite3"
    sidecar = UsageSidecar(path=path, enabled=True)

    assert (
        sidecar.record_and_check(
            day=DAY,
            tool="summon",
            variant="private query text",
            error=False,
            latency_ms=1,
            check_daily=True,
        )
        is False
    )
    assert not path.exists()


def test_high_version_is_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "usage.sqlite3"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sentinel(value TEXT)")
    connection.execute("PRAGMA user_version=2")
    connection.commit()
    connection.close()
    sidecar = UsageSidecar(path=path, enabled=True)

    assert (
        sidecar.record_and_check(
            day=DAY,
            tool="status",
            variant="default",
            error=False,
            latency_ms=1,
            check_daily=True,
        )
        is False
    )

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='sentinel'"
        ).fetchone()
    finally:
        connection.close()


def test_corrupt_or_locked_sidecar_degrades_without_overwrite(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt" / "usage.sqlite3"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"not a sqlite database")
    sidecar = UsageSidecar(path=corrupt, enabled=True)
    assert (
        sidecar.record_and_check(
            day=DAY,
            tool="status",
            variant="default",
            error=False,
            latency_ms=1,
            check_daily=True,
        )
        is False
    )
    assert corrupt.read_bytes() == b"not a sqlite database"

    locked = tmp_path / "locked" / "usage.sqlite3"
    owner = UsageSidecar(path=locked, enabled=True)
    owner.record_and_check(
        day="2026-08-20",
        tool="status",
        variant="default",
        error=False,
        latency_ms=1,
        check_daily=True,
    )
    connection = sqlite3.connect(locked, isolation_level=None)
    connection.execute("BEGIN EXCLUSIVE")
    try:
        assert (
            owner.record_and_check(
                day=DAY,
                tool="status",
                variant="default",
                error=False,
                latency_ms=1,
                check_daily=True,
            )
            is False
        )
    finally:
        connection.execute("ROLLBACK")
        connection.close()


def test_sidecar_schema_and_values_contain_no_content_fields(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "usage.sqlite3"
    secret = "synthetic-private-query-marker"
    sidecar = UsageSidecar(path=path, enabled=True)
    sidecar.record_and_check(
        day=DAY,
        tool="recall",
        variant="invalid",
        error=True,
        latency_ms=7,
        check_daily=False,
    )

    sql = " ".join(
        row["sql"] or ""
        for row in _rows(path, "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
    ).lower()
    for forbidden in (
        "query",
        "body",
        "title",
        "excerpt",
        "project",
        "agent",
        "record_id",
        "session",
    ):
        assert forbidden not in sql
    assert secret.encode() not in path.read_bytes()


def test_build_maintenance_warning_uses_only_low_ambiguity_rules() -> None:
    healthy = {
        "records": 154,
        "vectors": 154,
        "backlog": {"pending": 0, "permanent": 0},
        "curation_due": {"due": False, "reason": "none", "new_records": 15},
        "stage2_ready": {"ready": False},
    }
    assert build_maintenance_warning(healthy) is None

    waiting = {
        **healthy,
        "vectors": 153,
        "backlog": {"pending": 1, "permanent": 0},
    }
    assert build_maintenance_warning(waiting) is None

    unhealthy = {
        **healthy,
        "vectors": 150,
        "backlog": {"pending": 0, "permanent": 2},
        "curation_due": {
            "due": True,
            "reason": "new_records",
            "new_records": 20,
        },
    }
    assert build_maintenance_warning(unhealthy) == {
        "issues": [
            {"code": "permanent_backlog", "count": 2},
            {"code": "missing_vectors_without_backlog", "missing": 4},
            {"code": "curation_due", "reason": "new_records", "new_records": 20},
        ]
    }
