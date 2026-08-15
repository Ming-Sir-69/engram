import json
from pathlib import Path

from engram.cli import main

_IDEAS = """# 想法

## 2. 机会池

### 机械结构

- 折叠支架限位结构。
"""


def _sources(tmp_path: Path) -> Path:
    source = tmp_path / "second-brain"
    source.mkdir()
    (source / "ideas.md").write_text(_IDEAS, encoding="utf-8")
    (source / "inbox.md").write_text("# I\n\n## 待整理\n\n- 一条待办。\n", encoding="utf-8")
    (source / "knowledge.md").write_text(
        "# K\n\n## 1. 素材\n\n- **某项目**：见 https://example.com 。\n", encoding="utf-8"
    )
    (source / "projects.md").write_text(
        "# P\n\n## 进行中\n\n- **某项目**\n  - 状态：进行中\n", encoding="utf-8"
    )
    return source


def _taxonomy(tmp_path: Path) -> Path:
    """映射表走配置文件：仓库里不带任何私人笔记的标题。"""
    path = tmp_path / "seed_taxonomy.json"
    path.write_text(
        json.dumps(
            {
                "想法": {"domains": [], "tags": ["idea"]},
                "机会池": {"domains": [], "tags": ["opportunity"]},
                "机械结构": {
                    "domains": ["product-design"],
                    "tags": ["mechanism-design"],
                },
                "待整理": {"domains": [], "tags": ["inbox"]},
                "素材": {"domains": [], "tags": ["reference"]},
                "进行中": {"domains": ["tooling"], "tags": ["active"]},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _run(capsys, tmp_path: Path, *args: str) -> tuple[int, dict]:
    code = main(["--data-dir", str(tmp_path / "data"), *args])
    captured = capsys.readouterr()
    return code, json.loads(captured.out) if captured.out.strip() else {}


def test_migrate_reports_counts(capsys, tmp_path: Path) -> None:
    code, payload = _run(
        capsys, tmp_path, "migrate", "from-markdown", "--source", str(_sources(tmp_path)), "--taxonomy", str(_taxonomy(tmp_path))
    )
    assert code == 0
    assert payload["scanned"] == 4
    assert payload["created"] == 4
    assert payload["unseeded"] == 0


def test_migrate_dry_run_leaves_database_empty(capsys, tmp_path: Path) -> None:
    source = _sources(tmp_path)
    _, payload = _run(
        capsys, tmp_path, "migrate", "from-markdown", "--source", str(source), "--taxonomy", str(_taxonomy(tmp_path)), "--dry-run"
    )
    assert payload["created"] == 0
    _, status = _run(capsys, tmp_path, "status")
    assert status["records"] == 0


def test_migrate_missing_source_exits_with_invalid_input(capsys, tmp_path: Path) -> None:
    source = _sources(tmp_path)
    (source / "ideas.md").unlink()
    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "migrate",
            "from-markdown",
            "--source",
            str(source),
        ]
    )
    assert code == 65
    problem = json.loads(capsys.readouterr().err)
    assert problem["code"] == "SB-400-INVALID-INPUT"
    assert problem["context"]["missing"] == ["ideas.md"]


def test_export_markdown_round_trips_through_cli(capsys, tmp_path: Path) -> None:
    source = _sources(tmp_path)
    _run(capsys, tmp_path, "migrate", "from-markdown", "--source", str(source), "--taxonomy", str(_taxonomy(tmp_path)))
    out = tmp_path / "derived"
    code, payload = _run(capsys, tmp_path, "export", "markdown", "--out", str(out))
    assert code == 0
    assert payload["records"] == 4
    # 再导入一次导出结果：条数一致说明这条退路是通的。
    _, again = _run(capsys, tmp_path, "migrate", "from-markdown", "--source", str(out))
    assert again["created"] == 0


def test_export_refuses_to_write_into_the_source_dir(capsys, tmp_path: Path) -> None:
    source = _sources(tmp_path)
    _run(capsys, tmp_path, "migrate", "from-markdown", "--source", str(source), "--taxonomy", str(_taxonomy(tmp_path)))
    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "export",
            "markdown",
            "--out",
            str(source),
        ]
    )
    assert code == 65
