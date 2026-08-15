"""SQLite → Markdown / JSONL 的反向导出。

迁移之后 Markdown 变成派生视图，导出就是那条"随时能回到纯文本"的退路。
它保护两件事：**内容不会被锁死在数据库里**，以及**源文件不会被派生内容
覆盖**。后者靠派生标记实现——没有标记的文件一律不许写。
"""

import json
from pathlib import Path

import pytest

from engram.db import connect
from engram.errors import InvalidInputError
from engram.export import DERIVED_MARKER, export_jsonl, export_markdown
from engram.ingest.markdown import parse_markdown
from engram.ingest.migrate import migrate_from_markdown
from engram.migrations import migrate
from engram.repository import RecordRepository

_IDEAS = """# 想法

## 2. 机会池

### 机械结构

- 折叠支架限位结构。
- 阀盖：三段圆弧。
  - 子项也要跟着走。

### 外部项目

- **Tabula**：待评估适用范围。
"""

# 映射表是用户配置，测试自带一份合成的：真实标题属于私人笔记结构，不进仓库。
_TAXONOMY = {
    "想法": ((), ("idea",)),
    "机会池": ((), ("opportunity",)),
    "机械结构": (("product-design",), ("mechanism-design",)),
    "外部项目": (("tooling",), ("external-source",)),
    "待整理": ((), ("inbox",)),
    "素材": ((), ("reference",)),
    "进行中": (("tooling",), ("active",)),
}


def _sources(tmp_path: Path) -> Path:
    source = tmp_path / "second-brain"
    source.mkdir()
    (source / "ideas.md").write_text(_IDEAS, encoding="utf-8")
    (source / "inbox.md").write_text("# I\n\n## 待整理\n\n- 一条待办。\n", encoding="utf-8")
    (source / "knowledge.md").write_text(
        "# K\n\n## 1. 素材\n\n- **某素材**：见 https://example.com 。\n",
        encoding="utf-8",
    )
    (source / "projects.md").write_text(
        "# P\n\n## 进行中\n\n- **某项目**\n  - 状态：进行中\n", encoding="utf-8"
    )
    return source


def _migrated(tmp_path: Path) -> RecordRepository:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    repository = RecordRepository(connection)
    migrate_from_markdown(
        source_dir=_sources(tmp_path), repository=repository, taxonomy=_TAXONOMY
    )
    return repository


def test_export_writes_one_file_per_source(tmp_path: Path) -> None:
    repository = _migrated(tmp_path)
    out = tmp_path / "derived"
    report = export_markdown(repository=repository, out_dir=out)
    assert report["files"] == 4
    assert sorted(path.name for path in out.glob("*.md")) == [
        "ideas.md",
        "inbox.md",
        "knowledge.md",
        "projects.md",
    ]


def test_export_marks_files_as_derived(tmp_path: Path) -> None:
    """没有这行标记，几个月后没人分得清哪份是源文件。"""
    repository = _migrated(tmp_path)
    out = tmp_path / "derived"
    export_markdown(repository=repository, out_dir=out)
    first_line = (out / "ideas.md").read_text(encoding="utf-8").splitlines()[0]
    assert first_line == DERIVED_MARKER


def test_exported_markdown_reparses_to_the_same_entries(tmp_path: Path) -> None:
    """导出 → 重新解析 → 内容一致：这是"数据没被锁死"的机器证明。"""
    repository = _migrated(tmp_path)
    out = tmp_path / "derived"
    export_markdown(repository=repository, out_dir=out)

    stored = {
        (row["title"], row["body"])
        for row in repository.connection.execute("SELECT title, body FROM records")
    }
    reparsed = set()
    for path in sorted(out.glob("*.md")):
        for entry in parse_markdown(
            path.read_text(encoding="utf-8"), source_file=path.name
        ):
            reparsed.add((entry.title, entry.body))
    assert reparsed == stored


def test_exported_markdown_preserves_heading_path(tmp_path: Path) -> None:
    repository = _migrated(tmp_path)
    out = tmp_path / "derived"
    export_markdown(repository=repository, out_dir=out)
    entries = parse_markdown(
        (out / "ideas.md").read_text(encoding="utf-8"), source_file="ideas.md"
    )
    paths = {entry.title: entry.heading_path for entry in entries}
    assert paths["折叠支架限位结构"] == (
        "想法",
        "2. 机会池",
        "机械结构",
    )


def test_export_refuses_to_overwrite_unmarked_files(tmp_path: Path) -> None:
    """把源目录当成导出目录会直接销毁事实源，必须挡在写入之前。"""
    repository = _migrated(tmp_path)
    source = tmp_path / "second-brain"
    before = (source / "ideas.md").read_text(encoding="utf-8")
    with pytest.raises(InvalidInputError):
        export_markdown(repository=repository, out_dir=source)
    assert (source / "ideas.md").read_text(encoding="utf-8") == before


def test_export_overwrites_its_own_previous_output(tmp_path: Path) -> None:
    repository = _migrated(tmp_path)
    out = tmp_path / "derived"
    export_markdown(repository=repository, out_dir=out)
    report = export_markdown(repository=repository, out_dir=out)
    assert report["files"] == 4


def test_jsonl_export_has_one_line_per_record(tmp_path: Path) -> None:
    repository = _migrated(tmp_path)
    target = tmp_path / "records.jsonl"
    report = export_jsonl(repository=repository, out_file=target)
    lines = target.read_text(encoding="utf-8").splitlines()
    assert report["records"] == repository.count()
    assert len(lines) == repository.count()


def test_jsonl_export_carries_facets_and_provenance(tmp_path: Path) -> None:
    """导出若丢标签，重建就要重新跑一遍模型——退路必须是完整的。"""
    repository = _migrated(tmp_path)
    target = tmp_path / "records.jsonl"
    export_jsonl(repository=repository, out_file=target)
    payloads = [
        json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
    ]
    button = next(item for item in payloads if item["title"] == "折叠支架限位结构")
    assert button["attributes"]["source_file"] == "ideas.md"
    assert {facet["value"] for facet in button["facets"]} >= {"mechanism-design"}
    assert all(facet["provenance"] == "rule" for facet in button["facets"])
