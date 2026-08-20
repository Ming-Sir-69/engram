import json
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
