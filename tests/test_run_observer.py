from datetime import UTC, datetime, timedelta

from engram.run_observer import RunObserver

START = datetime(2026, 9, 20, 12, tzinfo=UTC)  # Sunday
DUE = datetime(2026, 9, 21, 1, tzinfo=UTC)  # Monday 09:00 Shanghai


def observer(tmp_path):
    return RunObserver(tmp_path, {"weekly": (0, 9, 0)}, now=START)


def test_records_cloud_never_started_and_deduplicates(tmp_path):
    o = observer(tmp_path)
    assert not o.check(now=DUE + timedelta(minutes=59))["recent"]
    r = o.check(now=DUE + timedelta(minutes=61))
    assert r["recent"][-1]["phase"] == "unconfirmed"
    before = o.path.read_bytes()
    assert len(o.check(now=DUE + timedelta(hours=3))["recent"]) == 1
    assert o.path.read_bytes() == before


def test_success_receipt_prevents_false_missing_alarm(tmp_path):
    o = observer(tmp_path)
    r = o.receipt("weekly", "start", now=DUE + timedelta(minutes=1))
    o.receipt("weekly", "success", run_id=r["run_id"], now=DUE + timedelta(minutes=3))
    assert all(
        e["phase"] != "unconfirmed"
        for e in o.check(now=DUE + timedelta(hours=2))["recent"]
    )


def test_failed_and_skipped_are_explicit_and_idempotent(tmp_path):
    o = observer(tmp_path)
    r = o.receipt("weekly", "start", now=DUE)
    end = o.receipt(
        "weekly",
        "skipped",
        run_id=r["run_id"],
        detail="model unavailable",
        now=DUE + timedelta(minutes=1),
    )
    again = o.receipt(
        "weekly", "success", run_id=r["run_id"], now=DUE + timedelta(minutes=2)
    )
    assert again["phase"] == end["phase"] == "skipped" and again["replayed"]
    assert (
        o.check(now=DUE + timedelta(hours=2))["recent"][-1]["detail"]
        == "model unavailable"
    )


def test_no_retroactive_alarm_before_activation(tmp_path):
    o = RunObserver(tmp_path, {"weekly": (0, 9, 0)}, now=DUE + timedelta(days=1))
    assert not o.check(now=DUE + timedelta(days=2))["recent"]


def test_manual_success_does_not_hide_next_scheduled_miss(tmp_path):
    o = observer(tmp_path)
    r = o.receipt("weekly", "start", now=START)
    assert r["slot"] is None
    o.receipt("weekly", "success", run_id=r["run_id"], now=START + timedelta(minutes=1))
    assert o.check(now=DUE + timedelta(hours=2))["recent"][-1]["phase"] == "unconfirmed"


def test_resume_only_catches_up_last_fourteen_days(tmp_path):
    o = observer(tmp_path)
    now = DUE + timedelta(days=27)
    events = o.check(now=now)["recent"]
    assert len(events) == 2
    assert all(datetime.fromisoformat(e["slot"]) >= now - timedelta(days=14) for e in events)


def test_daily_cloud_schedule_and_manual_receipt(tmp_path):
    at = datetime(2026, 10, 1, 0, tzinfo=UTC)
    o = RunObserver(tmp_path, {"cloud_daily": (-1, 9, 15), "manual": None}, now=at)
    start = o.receipt("manual", "start", now=at)
    assert start["slot"] is None
    o.receipt("manual", "success", run_id=start["run_id"], now=at)
    missed = o.check(now=at + timedelta(hours=3))["recent"][-1]
    assert missed["task"] == "cloud_daily"
    assert missed["slot"] == "2026-10-01T01:15:00+00:00"
    assert len([e for e in o.check(now=at + timedelta(days=1, hours=3))["recent"] if e["phase"] == "unconfirmed"]) == 2


def test_cloud_schedule_migration_does_not_create_historical_alarms(tmp_path):
    observer(tmp_path)
    later = DUE + timedelta(days=10)
    migrated = RunObserver(tmp_path, {"cloud_daily": (-1, 9, 15), "manual": None}, now=later)
    assert migrated.check(now=later)["recent"] == []


def test_late_start_gets_full_completion_window(tmp_path):
    o = observer(tmp_path)
    o.receipt("weekly", "start", now=DUE + timedelta(minutes=59))
    assert not [e for e in o.check(now=DUE + timedelta(minutes=61))["recent"] if e["phase"] == "unconfirmed"]
    assert o.check(now=DUE + timedelta(minutes=120))["recent"][-1]["phase"] == "unconfirmed"
