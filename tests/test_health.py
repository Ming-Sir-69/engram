from datetime import UTC, datetime, timedelta

import pytest

from engram.domain import RecordDraft
from engram.errors import InvalidInputError
from engram.health import _readonly, health_snapshot
from engram.maintenance import UsageSidecar
from engram.mcp.tools import ToolContext, call_tool

NOW = datetime(2026, 9, 16, 6, tzinfo=UTC)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAM_EXPORT_DIR", raising=False)
    monkeypatch.delenv("ENGRAM_INDEX_PATH", raising=False)
    context = ToolContext.open(data_dir=tmp_path / "engram", offline=True)
    yield context
    context.repository.connection.close()


def add(ctx, when, body):
    r = ctx.repository.create(RecordDraft(title="fixture", body=body))
    ctx.repository.connection.execute(
        "UPDATE records SET created_at=? WHERE record_id=?",
        (when.isoformat(), r.record_id),
    )
    return r.record_id


def test_health_readonly_counts_windows_and_no_content(ctx):
    one = add(ctx, NOW - timedelta(hours=1), "private-body-never-return")
    two = add(ctx, NOW - timedelta(days=4), "boundary-included")
    add(ctx, NOW - timedelta(days=4, seconds=1), "outside-four-days")
    add(ctx, NOW - timedelta(days=20), "outside-fourteen-days")
    c = ctx.repository.connection
    c.execute("UPDATE records SET status='archived' WHERE record_id=?", (two,))
    c.execute(
        "UPDATE outbox_jobs SET failure_kind='permanent' WHERE record_id=?", (one,)
    )
    c.execute(
        "INSERT INTO facets(record_id,kind,value,provenance,confidence) "
        "VALUES (?,'tag','private-tag','human',1)",
        (one,),
    )
    before = list(c.iterdump())
    snap = health_snapshot(ctx.config, window_days=14, now=NOW)
    assert list(c.iterdump()) == before
    assert snap["scope"] == "engram" and snap["model_used"] is False
    m = snap["metrics"]
    assert m["records"] == 4 and m["active_records"] == 3 and m["archived_records"] == 1
    assert m["new_in_window"] == 3 and m["new_last_4_days"] == 2
    assert m["active_islands"] == 3 and m["active_without_vector"] == 3
    assert m["backlog"]["permanent"] >= 1
    assert m["tags"] == {"distinct": 1, "single_record": 1}
    assert snap["integrity"]["state"] == "ok"
    assert not snap["usage"]["available"] and not snap["complete"]
    assert "private-body-never-return" not in str(snap)
    assert "private-tag" not in str(snap)
    assert "missing_active_vectors" in {f["code"] for f in snap["findings"]}


def test_midnight_timezone_and_zero_days(ctx):
    add(ctx, datetime(2026, 9, 15, 16, 10, tzinfo=UTC), "Shanghai Sep16")
    add(ctx, datetime(2026, 9, 15, 15, 59, tzinfo=UTC), "Shanghai Sep15")
    snap = health_snapshot(ctx.config, window_days=4, now=NOW)
    days = {x["day"]: x["count"] for x in snap["metrics"]["daily_new_records"]}
    assert days["2026-09-16"] == days["2026-09-15"] == 1
    assert days["2026-09-14"] == 0
    assert len(days) == 5  # partial first and last calendar days


def test_missing_database_is_unknown_and_not_created(ctx):
    ctx.repository.connection.close()
    ctx.config.db_path.unlink()
    snap = health_snapshot(ctx.config, now=NOW)
    assert not ctx.config.db_path.exists()
    assert snap["metrics"] is None and not snap["complete"]
    assert snap["integrity"]["state"] == "unknown"


def test_readonly_connection_rejects_writes(ctx):
    import sqlite3

    c = _readonly(ctx.config.db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            c.execute("DELETE FROM records")
    finally:
        c.close()


def test_usage_is_measured_not_user_activity_and_is_readonly(ctx):
    sidecar = UsageSidecar(ctx.config.usage_db_path)
    for name, error in [("status", False), ("get", True), ("remember", False)]:
        sidecar.record_and_check(
            day="2026-09-16",
            tool=name,
            variant="default",
            error=error,
            latency_ms=2,
            check_daily=False,
        )
    before = ctx.config.usage_db_path.read_bytes()
    snap = health_snapshot(ctx.config, now=NOW)
    assert snap["usage"]["calls"] == 3
    assert snap["usage"]["errors"] == 1
    assert snap["usage"]["non_status_calls"] == 2
    assert snap["complete"]
    assert ctx.config.usage_db_path.read_bytes() == before


def test_corrupt_usage_is_unknown(ctx):
    ctx.config.usage_db_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.config.usage_db_path.write_bytes(b"not a database")
    snap = health_snapshot(ctx.config, now=NOW)
    assert snap["metrics"] is not None
    assert not snap["complete"] and not snap["usage"]["available"]


@pytest.mark.parametrize("days", [0, 32, True, "4"])
def test_status_rejects_invalid_health_window(ctx, days):
    with pytest.raises(InvalidInputError):
        call_tool(ctx, "status", {"detail": "health", "window_days": days})


def test_status_health_is_opt_in_and_does_not_load_models(ctx, monkeypatch):
    monkeypatch.setattr(
        ToolContext, "_vector", lambda self: pytest.fail("model loaded")
    )
    before = list(ctx.repository.connection.iterdump())
    assert "metrics" not in call_tool(ctx, "status", {})
    snap = call_tool(ctx, "status", {"detail": "health", "window_days": 4})
    assert snap["scope"] == "engram" and snap["window"]["days"] == 4
    assert list(ctx.repository.connection.iterdump()) == before
