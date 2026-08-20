import json
import sqlite3
from pathlib import Path

from engram.cli import main


def run(capsys, tmp_path: Path, *args: str) -> tuple[int, dict]:
    code = main(["--data-dir", str(tmp_path), *args])
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else {}
    return code, payload


def test_create_returns_record_id(capsys, tmp_path: Path) -> None:
    code, payload = run(
        capsys, tmp_path, "record", "create", "--title", "灵感", "--body", "正文"
    )
    assert code == 0
    assert payload["record_id"].startswith("rec_")
    assert payload["revision"] == 1


def test_output_is_compact_json_by_default(capsys, tmp_path: Path) -> None:
    main(
        ["--data-dir", str(tmp_path), "record", "create", "--title", "t", "--body", "b"]
    )
    out = capsys.readouterr().out
    assert "\n  " not in out


def test_duplicate_content_returns_same_id(capsys, tmp_path: Path) -> None:
    _, first = run(capsys, tmp_path, "record", "create", "--title", "t", "--body", "b")
    _, second = run(capsys, tmp_path, "record", "create", "--title", "t", "--body", "b")
    assert first["record_id"] == second["record_id"]


def test_search_finds_created_record(capsys, tmp_path: Path) -> None:
    run(capsys, tmp_path, "record", "create", "--title", "负荷分级", "--body", "人因")
    code, payload = run(capsys, tmp_path, "search", "负荷分级", "--mode", "keyword")
    assert code == 0
    assert payload["results"][0]["title"] == "负荷分级"


def test_missing_record_exits_66(capsys, tmp_path: Path) -> None:
    code = main(["--data-dir", str(tmp_path), "record", "get", "rec_absent"])
    captured = capsys.readouterr()
    assert code == 66
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "SB-404-RECORD-NOT-FOUND"


def test_invalid_type_exits_65(capsys, tmp_path: Path) -> None:
    code = main(
        [
            "--data-dir",
            str(tmp_path),
            "record",
            "create",
            "--title",
            "t",
            "--body",
            "b",
            "--type",
            "idea",
        ]
    )
    assert code == 65


def test_status_reports_backlog(capsys, tmp_path: Path) -> None:
    run(capsys, tmp_path, "record", "create", "--title", "t", "--body", "b")
    code, payload = run(capsys, tmp_path, "status")
    assert code == 0
    assert payload["records"] == 1
    assert payload["backlog"]["pending"] == 1


def test_status_reports_evolution_triggers(capsys, tmp_path: Path) -> None:
    code, payload = run(capsys, tmp_path, "status")
    assert code == 0
    assert payload["curation_due"]["due"] is True
    assert payload["curation_due"]["reason"] == "never_curated"
    assert payload["stage2_ready"]["ready"] is False
    assert payload["stage2_ready"]["anchors_path"].endswith("eval-anchors.jsonl")


def test_create_reports_backlog_in_band(capsys, tmp_path: Path) -> None:
    _, payload = run(capsys, tmp_path, "record", "create", "--title", "t", "--body", "b")
    assert payload["backlog"]["pending"] >= 1


def test_projects_are_accepted(capsys, tmp_path: Path) -> None:
    _, payload = run(
        capsys,
        tmp_path,
        "record",
        "create",
        "--title",
        "t",
        "--body",
        "b",
        "--project",
        "engram",
        "--project",
        "jarvis-lite",
    )
    assert payload["projects"] == ["engram", "jarvis-lite"]


# ---- curate：确定性整理执行器 ----


def _open_db(tmp_path: Path) -> sqlite3.Connection:
    # autocommit：与 engram.db.connect 一致，否则未提交的隐式事务会
    # 持有写锁，CLI 侧的写入会撞 database is locked。
    connection = sqlite3.connect(
        tmp_path / "authoritative" / "engram.sqlite3", isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    return connection


def _tag(connection: sqlite3.Connection, record_id: str, value: str) -> None:
    connection.execute(
        "INSERT INTO facets(record_id, kind, value, provenance) "
        "VALUES (?, 'tag', ?, 'model')",
        (record_id, value),
    )


def _write_ops(tmp_path: Path, ops: list[dict]) -> str:
    ops_file = tmp_path / "ops.json"
    ops_file.write_text(json.dumps({"ops": ops}), encoding="utf-8")
    return str(ops_file)


def test_curate_dry_run_changes_nothing(capsys, tmp_path: Path) -> None:
    _, first = run(capsys, tmp_path, "record", "create", "--title", "a", "--body", "a")
    db = _open_db(tmp_path)
    _tag(db, first["record_id"], "alpha")
    ops_file = _write_ops(tmp_path, [{"op": "merge_tag", "from": "alpha", "to": "beta"}])

    code, payload = run(capsys, tmp_path, "curate", "apply", ops_file)

    assert code == 0
    assert payload["applied"] is False
    count = db.execute(
        "SELECT COUNT(*) FROM facets WHERE value = 'alpha'"
    ).fetchone()[0]
    assert count == 1
    assert db.execute("SELECT COUNT(*) FROM meta").fetchone()[0] == 0


def test_curate_merge_tag_handles_conflicts_and_stamps_meta(
    capsys, tmp_path: Path
) -> None:
    _, first = run(capsys, tmp_path, "record", "create", "--title", "a", "--body", "a")
    _, second = run(capsys, tmp_path, "record", "create", "--title", "b", "--body", "b")
    db = _open_db(tmp_path)
    _tag(db, first["record_id"], "alpha")
    _tag(db, first["record_id"], "beta")  # 冲突：first 已有目标标签
    _tag(db, second["record_id"], "alpha")
    ops_file = _write_ops(tmp_path, [{"op": "merge_tag", "from": "alpha", "to": "beta"}])

    code, payload = run(capsys, tmp_path, "curate", "apply", ops_file, "--apply")

    assert code == 0
    assert payload["applied"] is True
    rows = db.execute(
        "SELECT record_id, provenance, locked FROM facets WHERE value = 'beta' "
        "ORDER BY record_id"
    ).fetchall()
    assert [row["record_id"] for row in rows] == [
        first["record_id"],
        second["record_id"],
    ]
    # first 原有的 beta 保持不动；合并进来的行标记为人工裁决
    assert rows[1]["provenance"] == "human"
    assert db.execute(
        "SELECT COUNT(*) FROM facets WHERE value = 'alpha'"
    ).fetchone()[0] == 0
    # 受影响行有快照可回溯
    backups = list((tmp_path / "curate-backups").glob("*.jsonl"))
    assert len(backups) == 1
    assert "alpha" in backups[0].read_text(encoding="utf-8")
    # 整理状态与执行同事务落库，status 不再提示 due
    code, status = run(capsys, tmp_path, "status")
    assert status["curation_due"]["due"] is False


def test_curate_prune_links_keeps_human_edges(capsys, tmp_path: Path) -> None:
    _, first = run(capsys, tmp_path, "record", "create", "--title", "a", "--body", "a")
    _, second = run(capsys, tmp_path, "record", "create", "--title", "b", "--body", "b")
    db = _open_db(tmp_path)
    db.execute(
        "INSERT INTO record_links(source_id, target_id, relation, score, provenance) "
        "VALUES (?, ?, 'related_to', 0.3, 'knn')",
        (first["record_id"], second["record_id"]),
    )
    db.execute(
        "INSERT INTO record_links(source_id, target_id, relation, score, provenance) "
        "VALUES (?, ?, 'related_to', 0.3, 'human')",
        (second["record_id"], first["record_id"]),
    )
    ops_file = _write_ops(tmp_path, [{"op": "prune_links", "below": 0.5}])

    code, _ = run(capsys, tmp_path, "curate", "apply", ops_file, "--apply")

    assert code == 0
    rows = db.execute("SELECT provenance FROM record_links").fetchall()
    assert [row["provenance"] for row in rows] == ["human"]


def test_curate_delete_tag(capsys, tmp_path: Path) -> None:
    _, first = run(capsys, tmp_path, "record", "create", "--title", "a", "--body", "a")
    db = _open_db(tmp_path)
    _tag(db, first["record_id"], "junk")
    ops_file = _write_ops(tmp_path, [{"op": "delete_tag", "value": "junk"}])

    code, _ = run(capsys, tmp_path, "curate", "apply", ops_file, "--apply")

    assert code == 0
    assert db.execute("SELECT COUNT(*) FROM facets").fetchone()[0] == 0


def test_curate_set_tag_marks_human_and_locked(capsys, tmp_path: Path) -> None:
    _, first = run(capsys, tmp_path, "record", "create", "--title", "a", "--body", "a")
    ops_file = _write_ops(
        tmp_path,
        [
            {
                "op": "set_tag",
                "record_id": first["record_id"],
                "kind": "tag",
                "value": "curated",
            }
        ],
    )

    code, _ = run(capsys, tmp_path, "curate", "apply", ops_file, "--apply")

    assert code == 0
    row = _open_db(tmp_path).execute(
        "SELECT provenance, locked FROM facets WHERE value = 'curated'"
    ).fetchone()
    assert row["provenance"] == "human"
    assert row["locked"] == 1


def test_curate_rejects_unknown_op_without_applying_anything(
    capsys, tmp_path: Path
) -> None:
    _, first = run(capsys, tmp_path, "record", "create", "--title", "a", "--body", "a")
    db = _open_db(tmp_path)
    _tag(db, first["record_id"], "alpha")
    ops_file = _write_ops(
        tmp_path,
        [
            {"op": "merge_tag", "from": "alpha", "to": "beta"},
            {"op": "nuke_everything"},
        ],
    )

    code = main(["--data-dir", str(tmp_path), "curate", "apply", ops_file, "--apply"])

    assert code == 65
    # 校验前置：任何一个 op 非法，整批都不执行
    assert db.execute(
        "SELECT COUNT(*) FROM facets WHERE value = 'alpha'"
    ).fetchone()[0] == 1


def test_curate_set_tag_on_missing_record_exits_65(capsys, tmp_path: Path) -> None:
    ops_file = _write_ops(
        tmp_path,
        [{"op": "set_tag", "record_id": "rec_absent", "kind": "tag", "value": "x"}],
    )
    code = main(["--data-dir", str(tmp_path), "curate", "apply", ops_file, "--apply"])
    assert code == 65
