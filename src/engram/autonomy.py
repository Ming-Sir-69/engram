"""Autonomous, bounded maintenance with optimistic concurrency and rollback.

Model names are caller claims, not provider attestations. Safety comes from the
limited operation set and transaction checks; model selection belongs to the host.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta

import sqlite_vec

from engram.curate import _affected_rows, _execute, _validate
from engram.domain import content_hash_for
from engram.errors import InvalidInputError
from engram.tokenizer import fts_document

# Write-capable executors, in the configured fallback order. Review-only models
# (e.g. kimi-k3) are deliberately absent: they may label candidates, never write.
MODELS = {
    "gpt-6-astra",
    "gpt-5.6-sol",
    "claude-opus-5-5",
    "GPT-6 Astra",
    "GPT-5.6 Sol",
    "Claude Opus 5.5",
}
SOURCE_TABLES = (
    "records",
    "facets",
    "record_links",
    "revisions",
    "record_projects",
    "outbox_jobs",
)


def fingerprint(c):
    h = hashlib.sha256()
    for table in SOURCE_TABLES:
        rows = [tuple(r) for r in c.execute(f"SELECT * FROM {table}")]
        for row in sorted(json.dumps(r, ensure_ascii=False, default=str) for r in rows):
            h.update(table.encode() + row.encode())
    for r in c.execute(
        "SELECT record_id,model,dimensions,input_hash FROM embeddings ORDER BY record_id"
    ):
        h.update(json.dumps(tuple(r)).encode())
    return h.hexdigest()


def _root(config):
    root = config.data_dir / "runtime" / "autonomy"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _state(root):
    path = root / "state.json"
    return (
        json.loads(path.read_text())
        if path.exists()
        else {
            "failures": 0,
            "successes": 0,
            "batch_limit": 3,
            "open_until": None,
        }
    )


def _save(root, state, event):
    temporary = root / "state.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False))
    temporary.chmod(0o600)
    temporary.replace(root / "state.json")
    path = root / "events.jsonl"
    old = path.read_text().splitlines()[-99:] if path.exists() else []
    old.append(json.dumps(event, ensure_ascii=False))
    tmp = root / "events.tmp"
    tmp.write_text("\n".join(old) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def inspect(context):
    root = context.config.data_dir / "runtime" / "autonomy"
    state = (
        _state(root)
        if root.exists()
        else {
            "failures": 0,
            "successes": 0,
            "batch_limit": 3,
            "open_until": None,
        }
    )
    events = root / "events.jsonl"
    observer_root = os.environ.get("ENGRAM_OBSERVER_DIR")
    cloud_runs = None
    if observer_root:
        from pathlib import Path

        path = Path(observer_root) / "cloud-runs.json"
        if path.exists():
            observed = json.loads(path.read_text())
            cloud_runs = {
                "activated_at": observed["activated_at"],
                "recent": observed["events"][-12:],
            }
    return {
        "source_fingerprint": fingerprint(context.repository.connection),
        "enabled": os.environ.get("ENGRAM_AUTONOMOUS_MAINTENANCE") == "1",
        "state": state,
        "cloud_runs": cloud_runs,
        "recent": [json.loads(s) for s in events.read_text().splitlines()[-8:]]
        if events.exists()
        else [],
        "model_identity": "caller claim only; select and audit the actual model in cloud task settings",
        "coordination": "reuse existing curate operations and enrichment outbox; SQLite transaction plus source fingerprint",
    }


def _check(c):
    c.enable_load_extension(True)
    try:
        sqlite_vec.load(c)
    finally:
        c.enable_load_extension(False)
    if [r[0] for r in c.execute("PRAGMA integrity_check(10)")] != ["ok"]:
        raise InvalidInputError("integrity gate failed")
    if c.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise InvalidInputError("foreign-key gate failed")


def _record_op(c, op, stamp, executor="cloud-maintenance"):
    ident = op.get("record_id")
    row = c.execute("SELECT * FROM records WHERE record_id=?", (ident,)).fetchone()
    if row is None or op.get("expected_hash") != row["content_hash"]:
        raise InvalidInputError("record hash changed or record missing")
    if op["op"] == "revise_record":
        title, body = op.get("title"), op.get("body")
        if (
            not isinstance(title, str)
            or not isinstance(body, str)
            or not body.strip()
            or len(body) > 20000
            or len(title) > 240
        ):
            raise InvalidInputError("bounded non-empty revised content required")
        if re.search(r"sk-(?:proj-)?[A-Za-z0-9_-]{24,}", body):
            raise InvalidInputError("credential-like content rejected")
        digest = content_hash_for(title.strip(), body.strip())
        c.execute(
            "UPDATE records SET title=?,body=?,content_hash=?,revision=revision+1,updated_at=? WHERE record_id=?",
            (title.strip(), body.strip(), digest, stamp, ident),
        )
        c.execute("DELETE FROM records_fts WHERE record_id=?", (ident,))
        c.execute(
            "INSERT INTO records_fts(record_id,tokens) VALUES (?,?)",
            (ident, fts_document(title.strip() + "\n" + body.strip())),
        )
        c.execute("DELETE FROM embeddings WHERE record_id=?", (ident,))
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='vec_records'").fetchone():
            c.execute("DELETE FROM vec_records WHERE record_id=?", (ident,))
        # Existing pipeline owns embedding/classification/link completion.
        c.execute(
            "INSERT INTO outbox_jobs(record_id,job_type,next_attempt_at,created_at) VALUES (?,'enrich',?,?) "
            "ON CONFLICT(record_id,job_type) DO UPDATE SET attempts=0,next_attempt_at=excluded.next_attempt_at,failure_kind=NULL,last_error=NULL",
            (ident, stamp, stamp),
        )
    else:
        digest = row["content_hash"]
        archived = op["op"] == "archive_record"
        c.execute(
            "UPDATE records SET status=?,archived_at=?,revision=revision+1,updated_at=? WHERE record_id=?",
            (
                "archived" if archived else "active",
                stamp if archived else None,
                stamp,
                ident,
            ),
        )
    c.execute(
        "INSERT INTO revisions(record_id,revision,content_hash,changed_at,changed_by,summary) "
        "SELECT record_id,revision,?,?,?,? FROM records WHERE record_id=?",
        (digest, stamp, executor, op["op"], ident),
    )
    return {"op": op["op"], "record_id": ident}


def maintain(context, args):
    import fcntl

    if os.environ.get("ENGRAM_AUTONOMOUS_MAINTENANCE") != "1":
        raise InvalidInputError("autonomous maintenance is not enabled in this runtime")
    request_id = args.get("request_id", "")
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{8,80}", request_id
    ):
        raise InvalidInputError("request_id must be 8-80 safe characters")
    cloud_platform_claim = (
        args.get("model") == "chatgpt"
        and os.environ.get("ENGRAM_CLOUD_MAINTENANCE") == "1"
    )
    if args.get("model") not in MODELS and not cloud_platform_claim:
        raise InvalidInputError(
            "write-capable executors: GPT-6 Astra, GPT-5.6 Sol, Claude Opus 5.5; "
            "chatgpt platform claim requires ENGRAM_CLOUD_MAINTENANCE=1"
        )
    reason = args.get("reason", "")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise InvalidInputError("a bounded evidence-based reason is required")
    root = _root(context.config)
    lock = (root / "maintenance.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return {"applied": False, "state": "busy", "retry_later": True}
    c = context.repository.connection
    try:
        state = _state(root)
        receipt = c.execute(
            "SELECT value FROM meta WHERE key=?", ("autonomy_receipt_" + request_id,)
        ).fetchone()
        if receipt:
            return {**json.loads(receipt[0]), "replayed": True}
        events = root / "events.jsonl"
        previous = (
            [json.loads(s) for s in events.read_text().splitlines()]
            if events.exists()
            else []
        )
        matching = [e for e in previous if e["request_id"] == request_id]
        if matching:
            return {**matching[-1], "replayed": True}
        now = datetime.now(UTC)
        if (
            state.get("open_until")
            and datetime.fromisoformat(state["open_until"]) > now
        ):
            return {
                "applied": False,
                "state": "circuit_open",
                "open_until": state["open_until"],
            }
        ops = args.get("ops")
        if not isinstance(ops, list) or not 1 <= len(ops) <= state["batch_limit"]:
            raise InvalidInputError(
                f"ops must contain 1-{state['batch_limit']} operations"
            )
        if any(not isinstance(op, dict) for op in ops):
            raise InvalidInputError("operation must be an object")
        expected = args.get("expected_fingerprint")
        if expected != fingerprint(c):
            return {"applied": False, "state": "source_changed", "retry_later": True}
        _check(c)
        backup = root / f"before-{request_id}.sqlite3"
        dest = sqlite3.connect(backup)
        try:
            c.backup(dest)
        finally:
            dest.close()
        backup.chmod(0o600)
        stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        event = {
            "request_id": request_id,
            "at": now.isoformat(),
            "model_claim": args["model"],
            "before": expected,
            "operations": [op.get("op") for op in ops],
            "backup_id": request_id,
            "reason": re.sub(r"sk-[A-Za-z0-9_-]{16,}", "[redacted]", reason)[:500],
        }
        try:
            c.execute("BEGIN IMMEDIATE")
            if fingerprint(c) != expected:
                raise InvalidInputError("source changed while preparing backup")
            count = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            unlabelled_before = c.execute(
                "SELECT COUNT(*) FROM records r WHERE status='active' AND NOT EXISTS "
                "(SELECT 1 FROM facets f WHERE f.record_id=r.record_id)"
            ).fetchone()[0]
            islands_before = c.execute(
                "SELECT COUNT(*) FROM records r WHERE status='active' AND NOT EXISTS "
                "(SELECT 1 FROM record_links l WHERE l.source_id=r.record_id OR l.target_id=r.record_id)"
            ).fetchone()[0]
            reports = []
            affected_ids = set()
            source_limit = max(1, min(25, count // 10))
            for op in ops:
                if not isinstance(op, dict):
                    raise InvalidInputError("operation must be an object")
                name = op.get("op")
                if name == "restore_revision":
                    affected_ids.add(op.get("record_id"))
                    backup_id = op.get("backup_id", "")
                    if not isinstance(backup_id, str) or not re.fullmatch(
                        r"[A-Za-z0-9_-]{8,80}", backup_id
                    ):
                        raise InvalidInputError("invalid backup id")
                    owned = c.execute(
                        "SELECT value FROM meta WHERE key=?",
                        ("autonomy_receipt_" + backup_id,),
                    ).fetchone()
                    prior_path = root / f"before-{backup_id}.sqlite3"
                    if not owned or prior_path.is_symlink() or not prior_path.is_file():
                        raise InvalidInputError(
                            "verified maintenance backup not available"
                        )
                    prior = sqlite3.connect(prior_path.as_uri() + "?mode=ro", uri=True)
                    try:
                        old = prior.execute(
                            "SELECT title,body FROM records WHERE record_id=?",
                            (op.get("record_id"),),
                        ).fetchone()
                    finally:
                        prior.close()
                    if old is None:
                        raise InvalidInputError("record absent from verified backup")
                    restored = {
                        "op": "revise_record",
                        "record_id": op.get("record_id"),
                        "expected_hash": op.get("expected_hash"),
                        "title": old[0],
                        "body": old[1],
                    }
                    reports.append(_record_op(c, restored, stamp, f"maintenance:{args['model']}"))
                    continue
                if name in {"revise_record", "archive_record", "restore_record"}:
                    affected_ids.add(op.get("record_id"))
                    reports.append(_record_op(c, op, stamp, f"maintenance:{args['model']}"))
                elif name == "rebuild_fts":
                    rows = c.execute(
                        "SELECT record_id,title,body FROM records"
                    ).fetchall()
                    c.execute("DELETE FROM records_fts")
                    c.executemany(
                        "INSERT INTO records_fts(record_id,tokens) VALUES (?,?)",
                        [(r[0], fts_document(r[1] + "\n" + r[2])) for r in rows],
                    )
                    reports.append({"op": name, "rows": len(rows)})
                else:
                    _validate([op])
                    affected = _affected_rows(c, op)
                    merge_ids = set()
                    if name == "merge_tag":
                        merge_ids = {
                            r["record_id"] for r in affected if r["value"] == op["from"]
                        }
                        affected = [r for r in affected if r["record_id"] in merge_ids]
                    if name == "set_tag":
                        affected = [
                            r for r in affected if r.get("value") == op.get("value")
                        ]
                    if len(affected) > 25 or any(
                        r.get("provenance") == "human"
                        or (r.get("locked") and r.get("provenance") != "model")
                        for r in affected
                    ):
                        raise InvalidInputError(
                            "batch too broad or touches locked facets; replan"
                        )
                    for row in affected:
                        affected_ids.update(
                            row[k]
                            for k in ("record_id", "source_id", "target_id")
                            if k in row
                        )
                    if name == "set_tag":
                        affected_ids.add(op.get("record_id"))
                    report = _execute(c, op)
                    if name == "set_tag":
                        c.execute(
                            "UPDATE facets SET provenance='model' WHERE record_id=? AND kind=? AND value=?",
                            (op["record_id"], op.get("kind", "tag"), op["value"]),
                        )
                    elif name == "merge_tag":
                        for ident in merge_ids:
                            c.execute(
                                "UPDATE facets SET provenance='model' WHERE kind='tag' AND value=? AND record_id=? AND provenance='human'",
                                (op["to"], ident),
                            )
                    reports.append(report)
            if len(affected_ids) > source_limit:
                raise InvalidInputError(
                    "affected records exceed batch scope; split the plan"
                )
            _check(c)
            if c.execute("SELECT COUNT(*) FROM records").fetchone()[0] != count:
                raise InvalidInputError("physical record deletion is forbidden")
            missing = c.execute(
                "SELECT COUNT(*) FROM records r WHERE NOT EXISTS (SELECT 1 FROM records_fts f WHERE f.record_id=r.record_id)"
            ).fetchone()[0]
            if missing:
                raise InvalidInputError("FTS coverage check failed")
            unlabelled_after = c.execute(
                "SELECT COUNT(*) FROM records r WHERE status='active' AND NOT EXISTS "
                "(SELECT 1 FROM facets f WHERE f.record_id=r.record_id)"
            ).fetchone()[0]
            if unlabelled_after > unlabelled_before and not any(
                op["op"] == "restore_record" for op in ops
            ):
                raise InvalidInputError(
                    "maintenance removed the last label from active records"
                )
            islands_after = c.execute(
                "SELECT COUNT(*) FROM records r WHERE status='active' AND NOT EXISTS "
                "(SELECT 1 FROM record_links l WHERE l.source_id=r.record_id OR l.target_id=r.record_id)"
            ).fetchone()[0]
            if islands_after > islands_before and not any(
                op["op"] == "restore_record" for op in ops
            ):
                raise InvalidInputError("maintenance introduced new active islands")
            event.update(
                applied=True,
                state="verified",
                after=fingerprint(c),
                reports=reports,
                affected_record_ids=sorted(affected_ids),
                verification={
                    "transaction_integrity": True,
                    "physical_records_preserved": True,
                    "semantic_correctness": "not_machine_verified",
                    "retrieval_quality": "not_measured",
                },
            )
            # Durable commit receipt is in the same source transaction.
            c.execute(
                "INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    "autonomy_receipt_" + request_id,
                    json.dumps(event, ensure_ascii=False),
                ),
            )
            c.execute("COMMIT")
            state.update(failures=0, successes=state["successes"] + 1, open_until=None)
            state["batch_limit"] = min(5, 3 + state["successes"] // 3)
        except Exception as error:  # noqa: BLE001 - rollback every failed maintenance transaction
            if c.in_transaction:
                c.execute("ROLLBACK")
            state["failures"] += 1
            state["batch_limit"] = 1
            if state["failures"] >= 3:
                state["open_until"] = (now + timedelta(hours=6)).isoformat()
            event.update(
                applied=False,
                state="rolled_back",
                error_type=type(error).__name__,
                rejection=error.detail
                if isinstance(error, InvalidInputError)
                else "transaction verification failed",
            )
        _save(root, state, event)
        return event
    finally:
        lock.close()
