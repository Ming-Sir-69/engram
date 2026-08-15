"""Markdown → SQLite 的一次性迁移。

迁移之后 SQLite 成为事实源，Markdown 变成派生视图。因此这里的每一条断言
都在保护一件事：**迁移不能丢信息，也不能重复写入**。行号、标题路径、种子
标签都必须原样落库，重跑必须零新增。
"""

from pathlib import Path

import pytest

from engram.db import connect
from engram.errors import InvalidInputError
from engram.ingest.migrate import migrate_from_markdown
from engram.migrations import migrate
from engram.repository import RecordRepository

_IDEAS = """# 想法

## 2. 机会池

记录模板：

- 点子名称：
- 客户是谁：

### 机械结构

- 阀盖：三段圆弧；旋钮解锁弹簧压紧。
- 折叠支架限位结构。

### 外部项目

- **Tabula**：待评估适用范围。
"""

_PROJECTS = """# 项目

## 进行中

- **定时资讯汇编脚本**
  - 状态：进行中
  - 更新：2026-01-04
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
    (source / "projects.md").write_text(_PROJECTS, encoding="utf-8")
    (source / "inbox.md").write_text("# Inbox\n\n## 待整理\n\n- 一条待办。\n", encoding="utf-8")
    (source / "knowledge.md").write_text(
        "# 知识\n\n## 1. 素材\n\n- **某项目**：见 https://example.com 。\n",
        encoding="utf-8",
    )
    return source


def _repository(tmp_path: Path) -> RecordRepository:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    return RecordRepository(connection)


def test_every_top_level_bullet_becomes_a_record(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    report = migrate_from_markdown(source_dir=_sources(tmp_path), repository=repository, taxonomy=_TAXONOMY)
    # 2 条机械结构 + 1 条外部项目 + 1 条项目 + 1 条 inbox + 1 条素材 = 6
    assert report.scanned == 6
    assert report.created == 6
    assert repository.count() == 6


def test_template_lines_never_become_records(tmp_path: Path) -> None:
    """`记录模板：` 下面是书写脚手架，不是内容。"""
    repository = _repository(tmp_path)
    migrate_from_markdown(source_dir=_sources(tmp_path), repository=repository, taxonomy=_TAXONOMY)
    titles = [
        row["title"]
        for row in repository.connection.execute("SELECT title FROM records").fetchall()
    ]
    assert "点子名称" not in titles
    assert "客户是谁" not in titles


def test_seed_facets_are_written_with_rule_provenance(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    migrate_from_markdown(source_dir=_sources(tmp_path), repository=repository, taxonomy=_TAXONOMY)
    row = repository.connection.execute(
        "SELECT record_id FROM records WHERE title LIKE ?", ("阀盖%",)
    ).fetchone()
    facets = repository.trusted_facets(row["record_id"])
    assert {(f.kind, f.value) for f in facets} >= {
        ("domain", "product-design"),
        ("tag", "mechanism-design"),
    }
    assert all(f.provenance == "rule" for f in facets)
    assert all(f.confidence == pytest.approx(0.9) for f in facets)


def test_rerun_creates_nothing_new(tmp_path: Path) -> None:
    """迁移必须可重跑：内容哈希相同就不该再写一条。"""
    repository = _repository(tmp_path)
    source = _sources(tmp_path)
    migrate_from_markdown(source_dir=source, repository=repository, taxonomy=_TAXONOMY)
    second = migrate_from_markdown(source_dir=source, repository=repository, taxonomy=_TAXONOMY)
    assert second.created == 0
    assert second.existing == 6
    assert repository.count() == 6


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    report = migrate_from_markdown(
        source_dir=_sources(tmp_path), repository=repository, taxonomy=_TAXONOMY, dry_run=True
    )
    assert report.scanned == 6
    assert report.created == 0
    assert repository.count() == 0


def test_provenance_records_source_file_and_line_range(tmp_path: Path) -> None:
    """行号是回查原文的唯一锚点，迁移丢了就再也补不回来。"""
    repository = _repository(tmp_path)
    migrate_from_markdown(source_dir=_sources(tmp_path), repository=repository, taxonomy=_TAXONOMY)
    row = repository.connection.execute(
        "SELECT record_id FROM records WHERE title = ?", ("折叠支架限位结构",)
    ).fetchone()
    attributes = repository.get(row["record_id"]).attributes
    assert attributes["source_file"] == "ideas.md"
    assert attributes["start_line"] == attributes["end_line"]
    assert attributes["heading_path"] == [
        "想法",
        "2. 机会池",
        "机械结构",
    ]


def test_project_entries_get_project_record_type(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    migrate_from_markdown(source_dir=_sources(tmp_path), repository=repository, taxonomy=_TAXONOMY)
    row = repository.connection.execute(
        "SELECT record_type FROM records WHERE title = ?", ("定时资讯汇编脚本",)
    ).fetchone()
    assert row["record_type"] == "project"


def test_external_source_entries_get_reference_record_type(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    migrate_from_markdown(source_dir=_sources(tmp_path), repository=repository, taxonomy=_TAXONOMY)
    row = repository.connection.execute(
        "SELECT record_type FROM records WHERE title = ?", ("Tabula",)
    ).fetchone()
    assert row["record_type"] == "reference"


def test_recorded_at_is_omitted_when_absent(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    migrate_from_markdown(source_dir=_sources(tmp_path), repository=repository, taxonomy=_TAXONOMY)
    row = repository.connection.execute(
        "SELECT record_id FROM records WHERE title = ?", ("定时资讯汇编脚本",)
    ).fetchone()
    assert "recorded_at" not in repository.get(row["record_id"]).attributes


def test_missing_source_file_is_an_error(tmp_path: Path) -> None:
    """静默跳过会产生"看起来成功"的半截迁移，比直接失败更危险。"""
    source = _sources(tmp_path)
    (source / "inbox.md").unlink()
    with pytest.raises(InvalidInputError):
        migrate_from_markdown(source_dir=source, repository=_repository(tmp_path), taxonomy=_TAXONOMY)


def test_body_round_trips_to_the_original_block(tmp_path: Path) -> None:
    """反向导出依赖这条不变量：加回 `- ` 前缀就是原文。"""
    repository = _repository(tmp_path)
    source = _sources(tmp_path)
    migrate_from_markdown(source_dir=source, repository=repository, taxonomy=_TAXONOMY)
    lines = (source / "projects.md").read_text(encoding="utf-8").splitlines()
    row = repository.connection.execute(
        "SELECT record_id FROM records WHERE title = ?", ("定时资讯汇编脚本",)
    ).fetchone()
    record = repository.get(row["record_id"])
    start = int(record.attributes["start_line"])
    end = int(record.attributes["end_line"])
    assert "- " + record.body == "\n".join(lines[start - 1 : end])


def test_migration_queues_embedding_work(tmp_path: Path) -> None:
    """种子标签免掉了分类，但向量必须补——否则语义检索是瞎的。"""
    repository = _repository(tmp_path)
    migrate_from_markdown(source_dir=_sources(tmp_path), repository=repository, taxonomy=_TAXONOMY)
    assert repository.backlog()["pending"] == 6
