import json
import sqlite3

import pytest

from engram.autonomy import fingerprint, inspect, maintain
from engram.domain import RecordDraft
from engram.errors import InvalidInputError
from engram.mcp.tools import ToolContext
from engram.review import review_snapshot


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAM_AUTONOMOUS_MAINTENANCE", "1")
    monkeypatch.delenv("ENGRAM_EXPORT_DIR", raising=False)
    monkeypatch.delenv("ENGRAM_INDEX_PATH", raising=False)
    c = ToolContext.open(data_dir=tmp_path, offline=True)
    c.repository.create(RecordDraft(title="alpha", body="private fixture"))
    yield c
    c.repository.connection.close()


def request(ctx, ops, ident="request-one"):
    return {
        "request_id": ident,
        "expected_fingerprint": fingerprint(ctx.repository.connection),
        "model": "gpt-6-astra",
        "reason": "fixture evidence",
        "ops": ops,
    }


def test_disabled_and_low_model_rejected(ctx, monkeypatch):
    r = request(ctx, [{"op": "rebuild_fts"}])
    monkeypatch.delenv("ENGRAM_AUTONOMOUS_MAINTENANCE")
    with pytest.raises(InvalidInputError):
        maintain(ctx, r)
    monkeypatch.setenv("ENGRAM_AUTONOMOUS_MAINTENANCE", "1")
    r["model"] = "gpt-5.6-luna"
    with pytest.raises(InvalidInputError):
        maintain(ctx, r)


def test_atomic_repair_backup_and_receipt_survive_sidecar_loss(ctx):
    c = ctx.repository.connection
    c.execute("DELETE FROM records_fts")
    r = request(ctx, [{"op": "rebuild_fts"}])
    result = maintain(ctx, r)
    assert result["applied"] and result["state"] == "verified"
    assert c.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0] == 1
    root = ctx.config.data_dir / "runtime/autonomy"
    assert (root / "before-request-one.sqlite3").exists()
    (root / "events.jsonl").unlink()
    replay = maintain(ctx, r)
    assert replay["replayed"] and replay["applied"]


def test_concurrent_source_change_never_overwrites(ctx):
    r = request(ctx, [{"op": "rebuild_fts"}])
    ctx.repository.create(RecordDraft(title="new", body="concurrent record"))
    result = maintain(ctx, r)
    assert result["state"] == "source_changed" and not result["applied"]
    assert ctx.repository.count() == 2


def test_failure_rolls_back_entire_batch_and_opens_circuit(ctx):
    c = ctx.repository.connection
    before = list(c.iterdump())
    for i in range(3):
        result = maintain(
            ctx,
            request(
                ctx,
                [{"op": "rebuild_fts"}, {"op": "unknown"}][
                    : inspect(ctx)["state"]["batch_limit"]
                ]
                if i == 0
                else [{"op": "unknown"}],
                f"failure-{i:03}",
            ),
        )
        assert result["state"] == "rolled_back"
        assert list(c.iterdump()) == before
    assert inspect(ctx)["state"]["open_until"]
    assert (
        maintain(ctx, request(ctx, [{"op": "rebuild_fts"}], "after-trip"))["state"]
        == "circuit_open"
    )


def test_revision_preserves_record_and_queues_existing_pipeline(ctx):
    c = ctx.repository.connection
    r = c.execute("SELECT * FROM records").fetchone()
    op = {
        "op": "revise_record",
        "record_id": r["record_id"],
        "expected_hash": r["content_hash"],
        "title": "corrected",
        "body": "verified replacement",
    }
    result = maintain(ctx, request(ctx, [op]))
    assert result["applied"], result
    assert ctx.repository.count() == 1
    row = ctx.repository.get(r["record_id"])
    assert row.revision == 2 and row.body == "verified replacement"
    assert c.execute("SELECT COUNT(*) FROM outbox_jobs").fetchone()[0] == 1
    backup = sqlite3.connect(
        ctx.config.data_dir / "runtime/autonomy/before-request-one.sqlite3"
    )
    assert backup.execute("SELECT body FROM records").fetchone()[0] == "private fixture"
    backup.close()
    revised = ctx.repository.get(r["record_id"])
    restored = maintain(
        ctx,
        request(
            ctx,
            [
                {
                    "op": "restore_revision",
                    "record_id": r["record_id"],
                    "expected_hash": revised.content_hash,
                    "backup_id": "request-one",
                }
            ],
            "restore-revision",
        ),
    )
    assert restored["applied"], restored
    assert ctx.repository.get(r["record_id"]).body == "private fixture"


def test_mcp_maintenance_syncs_through_existing_export(ctx, monkeypatch):
    from dataclasses import replace

    from engram.mcp.tools import call_tool

    target = ctx.config.data_dir / "test-derived"
    ctx.config = replace(ctx.config, export_dir=target)
    r = call_tool(ctx, "maintain", request(ctx, [{"op": "rebuild_fts"}]))
    assert r["applied"] and r["sync"]["ok"]
    assert target.exists()


def test_archival_and_restore_are_reversible_without_deletion(ctx):
    row = ctx.repository.connection.execute("SELECT * FROM records").fetchone()
    op = {
        "op": "archive_record",
        "record_id": row["record_id"],
        "expected_hash": row["content_hash"],
    }
    assert maintain(ctx, request(ctx, [op]))["applied"]
    assert ctx.repository.get(row["record_id"]).status == "archived"
    op["op"] = "restore_record"
    assert maintain(ctx, request(ctx, [op], "restore-one"))["applied"]
    assert ctx.repository.get(row["record_id"]).status == "active"
    assert ctx.repository.count() == 1


def test_dont_strip_last_label_or_overwrite_human(ctx):
    c = ctx.repository.connection
    ident = c.execute("SELECT record_id FROM records").fetchone()[0]
    c.execute(
        "INSERT INTO facets(record_id,kind,value,provenance,confidence) VALUES (?,'tag','only-tag','model',1)",
        (ident,),
    )
    result = maintain(ctx, request(ctx, [{"op": "delete_tag", "value": "only-tag"}]))
    assert not result["applied"]
    c.execute("UPDATE facets SET provenance='human'")
    result = maintain(
        ctx,
        request(
            ctx,
            [{"op": "merge_tag", "from": "only-tag", "to": "new-tag"}],
            "human-check",
        ),
    )
    assert not result["applied"]
    assert c.execute("SELECT value FROM facets").fetchone()[0] == "only-tag"


def test_review_evidence_is_readonly_bounded_and_has_fingerprint(ctx):
    c = ctx.repository.connection
    before = list(c.iterdump())
    r = review_snapshot(ctx.config, limit=1)
    assert r["review"]["state"] == "measured"
    assert len(r["review"]["candidates"]) == 1
    assert r["review"]["source_fingerprint"] == fingerprint(c)
    assert "private fixture" not in json.dumps(r)
    assert list(c.iterdump()) == before


def test_executor_whitelist_and_revision_attribution(ctx):
    c = ctx.repository.connection
    r = c.execute("SELECT * FROM records").fetchone()
    review_only = request(ctx, [{"op": "rebuild_fts"}], ident="review-only-1")
    review_only["model"] = "kimi-k3"
    with pytest.raises(InvalidInputError):
        maintain(ctx, review_only)
    op = {
        "op": "revise_record",
        "record_id": r["record_id"],
        "expected_hash": r["content_hash"],
        "title": "corrected by fallback",
        "body": "verified replacement",
    }
    req = request(ctx, [op], ident="opus-fallback-1")
    req["model"] = "claude-opus-5-5"
    result = maintain(ctx, req)
    assert result["applied"], result
    by = c.execute(
        "SELECT changed_by FROM revisions WHERE record_id=? ORDER BY revision DESC",
        (r["record_id"],),
    ).fetchone()[0]
    assert by == "maintenance:claude-opus-5-5"


def test_fts_repair_loads_extension_once_and_keeps_both_integrity_gates(ctx, monkeypatch):
    """Synthetic FTS mutation must not re-register sqlite-vec inside the transaction."""
    from engram import autonomy

    c = ctx.repository.connection
    c.execute("DELETE FROM records_fts")
    original_load = autonomy.sqlite_vec.load
    loads = []
    checks = []

    def traced_load(connection):
        loads.append(connection.in_transaction)
        return original_load(connection)

    monkeypatch.setattr(autonomy.sqlite_vec, "load", traced_load)
    c.set_trace_callback(lambda sql: checks.append(sql) if sql.startswith("PRAGMA ") else None)
    try:
        result = maintain(ctx, request(ctx, [{"op": "rebuild_fts"}], "synthetic-extension-once"))
    finally:
        c.set_trace_callback(None)
    assert result["applied"], result
    assert loads == [False]
    assert checks.count("PRAGMA integrity_check(10)") == 2
    assert checks.count("PRAGMA foreign_key_check") == 2
    assert c.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0] == 1


def test_pre_write_foreign_key_gate_rejects_synthetic_orphan(ctx):
    c = ctx.repository.connection
    c.execute("PRAGMA foreign_keys=OFF")
    c.execute("INSERT INTO record_projects(record_id,project) VALUES (?,?)",
              ("synthetic-missing-parent", "synthetic-project"))
    c.execute("PRAGMA foreign_keys=ON")
    before = fingerprint(c)
    with pytest.raises(InvalidInputError, match="foreign-key gate failed"):
        maintain(ctx, request(ctx, [{"op": "rebuild_fts"}], "synthetic-pre-gate"))
    assert fingerprint(c) == before
    assert c.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0] == 1


def test_post_write_foreign_key_gate_rolls_back_synthetic_fault(ctx, monkeypatch):
    from engram import autonomy

    c = ctx.repository.connection
    original_tokenize = autonomy.fts_document
    before = fingerprint(c)

    def insert_deferred_synthetic_fault(text):
        # Simulate a failing extension/operation with a real SQLite FK violation.
        c.execute("PRAGMA defer_foreign_keys=ON")
        c.execute("INSERT INTO record_projects(record_id,project) VALUES (?,?)",
                  ("synthetic-missing-parent", "synthetic-project"))
        return original_tokenize(text)

    monkeypatch.setattr(autonomy, "fts_document", insert_deferred_synthetic_fault)
    result = maintain(ctx, request(ctx, [{"op": "rebuild_fts"}], "synthetic-post-gate"))
    assert result["applied"] is False and result["state"] == "rolled_back"
    assert result["rejection"] == "foreign-key gate failed"
    assert fingerprint(c) == before
    assert c.execute("PRAGMA foreign_key_check").fetchone() is None
    assert c.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0] == 1
