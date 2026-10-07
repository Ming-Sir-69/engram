from datetime import UTC, datetime, timedelta

from engram.feedback import FeedbackStore, MCPObservability


def test_diagnostic_is_closed_vocabulary_without_values(tmp_path):
    from engram.errors import InvalidInputError
    from engram.feedback import validation_diagnostic

    private = "private user text"
    error = InvalidInputError(private, context={"argument": "body"})
    diagnostic = validation_diagnostic({"body": [private]}, error)
    assert diagnostic == {"code": "invalid_type", "field": "body", "input_type": "list"}
    store = FeedbackStore(tmp_path / "feedback.sqlite3")
    diagnostic.update(body=private, extra=private)
    assert store.record_event(
        tool="remember",
        variant="default",
        ok=False,
        latency_ms=1,
        error_type="InvalidInputError",
        diagnostic=diagnostic,
    )
    result = store.snapshot(now=datetime.now(UTC) + timedelta(seconds=1))
    assert result["recent_errors"][0]["diagnostic"]["field"] == "body"
    assert private not in str(result)


def test_old_schema_read_does_not_require_migration(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    store = FeedbackStore(path)
    store.record_event(tool="remember", variant="default", ok=False, latency_ms=1)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE runtime_events DROP COLUMN diagnostic")
    assert store.snapshot()["available"]
    assert store.record_event(
        tool="remember",
        variant="default",
        ok=False,
        latency_ms=1,
        diagnostic={"field": "body", "code": "missing"},
    )


def test_feedback_inbox_redacts_and_updates(tmp_path):
    store = FeedbackStore(tmp_path / "runtime" / "feedback.sqlite3")
    item = store.add(
        summary="remember failed with token=secret-value",
        category="reliability",
        severity="high",
        expected="one result",
        actual="error",
        tool="remember",
    )
    assert item["feedback_id"].startswith("fb_")
    assert "[redacted]" in item["summary"]
    updated = store.update(
        item["feedback_id"],
        status="investigating",
        hypothesis="transient database lock",
    )
    assert updated["status"] == "investigating"
    assert store.list(status="investigating")[0]["feedback_id"] == item["feedback_id"]


def test_runtime_snapshot_aggregates_without_bodies(tmp_path):
    store = FeedbackStore(tmp_path / "runtime" / "feedback.sqlite3")
    for ok, latency in [(True, 10), (True, 20), (False, 2000), (False, 3000)]:
        assert store.record_event(
            tool="recall",
            variant="keyword",
            ok=ok,
            latency_ms=latency,
            error_type="InvalidInputError" if not ok else "",
        )
    store.add(summary="结果与预期不同", category="quality")
    now = datetime.now(UTC) + timedelta(seconds=1)
    snapshot = store.snapshot(now=now)
    assert snapshot["available"] is True
    assert snapshot["events"] == {"calls": 4, "errors": 2, "error_rate": 0.5}
    assert snapshot["by_tool"][0]["tool"] == "recall"
    assert snapshot["by_variant"][0]["variant"] == "keyword"
    assert snapshot["feedback"]["open"] == 1
    assert any(
        s["code"] == "open_feedback" for s in snapshot["reinforcement"]["signals"]
    )
    assert "query" not in str(snapshot)


def test_missing_store_is_read_only_and_does_not_create(tmp_path):
    path = tmp_path / "runtime" / "feedback.sqlite3"
    snapshot = FeedbackStore(path).snapshot()
    assert snapshot["available"] is False
    assert not path.exists()


def test_mcp_services_share_store_but_reports_are_scoped(tmp_path):
    path = tmp_path / "mcp-feedback.sqlite3"
    first = MCPObservability("mingle-lens", instance="local", path=path)
    second = MCPObservability("other-mcp", instance="remote", path=path)
    assert first.record_event(tool="read_file", ok=True, latency_ms=4)
    assert second.record_event(
        tool="search", ok=False, latency_ms=12, error_type="Timeout"
    )
    first.submit_feedback(summary="只读工具返回超时", category="reliability")
    assert first.report(window_days=1)["events"]["calls"] == 1
    all_services = FeedbackStore(path).snapshot(window_days=1)
    assert {x["service"] for x in all_services["by_service"]} == {
        "mingle-lens",
        "other-mcp",
    }
    assert all_services["feedback"]["open"] == 1


def test_schema_upgrade_and_events_are_safe_under_concurrency(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "upgrade.sqlite3"

    def write(index):
        return FeedbackStore(path).record_event(
            service="new-mcp",
            tool=f"tool-{index % 2}",
            variant="default",
            ok=True,
            latency_ms=index,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(24)))
    assert all(results)
    assert FeedbackStore(path).snapshot(window_days=1)["events"]["calls"] == 24
