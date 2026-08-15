"""写入后自动回写 Markdown。

事实源换成 SQLite 之后，纯文本备份不能再依赖"记得去导一次"。这里测的是
那条自动化链路：**配置了导出目录，写入就同步**，而且同步的失败绝不允许
反过来伤害写入——后者是这个库唯一不容退让的保证。

同步做在库内部而不是宿主的 Hook 或额外工具里，这样 CLI 与 MCP 两条路径
行为一致，调用方也不需要知道它的存在。
"""

import json
from pathlib import Path

import pytest

from engram.config import load_config
from engram.db import connect
from engram.domain import RecordDraft
from engram.errors import InvalidInputError
from engram.export import DERIVED_MARKER, export_markdown
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.sync import sync_derived


@pytest.fixture
def repository(tmp_path: Path) -> RecordRepository:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    return RecordRepository(connection)


def _config(tmp_path: Path, export_dir: Path | None):
    env = {"ENGRAM_DATA_DIR": str(tmp_path / "data")}
    if export_dir is not None:
        env["ENGRAM_EXPORT_DIR"] = str(export_dir)
    return load_config(env=env)


def test_sync_stays_off_until_a_target_is_configured(
    tmp_path: Path, repository: RecordRepository
) -> None:
    """没配置就什么都不做——默认不去碰用户的任何目录。"""
    repository.create(RecordDraft(title="t", body="b"))
    assert sync_derived(config=_config(tmp_path, None), repository=repository) is None


def test_sync_writes_the_new_record_into_markdown(
    tmp_path: Path, repository: RecordRepository
) -> None:
    out = tmp_path / "derived"
    repository.create(RecordDraft(title="限位结构", body="折叠支架的限位结构"))
    result = sync_derived(config=_config(tmp_path, out), repository=repository)

    assert result["ok"] is True
    written = "".join(path.read_text(encoding="utf-8") for path in out.glob("*.md"))
    assert "折叠支架的限位结构" in written


def test_a_broken_target_never_breaks_the_write(
    tmp_path: Path, repository: RecordRepository
) -> None:
    """同步失败必须降级为一条报告，而不是异常。

    写入永不失败是这个库的底线；让备份机制有能力否决写入，等于把底线
    交给一个可选功能。
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("我是个文件，不是目录", encoding="utf-8")

    result = sync_derived(config=_config(tmp_path, blocked), repository=repository)

    assert result["ok"] is False
    assert result["error"]


def test_sync_refuses_a_directory_it_did_not_generate(
    tmp_path: Path, repository: RecordRepository
) -> None:
    """误把源目录配成导出目录是最贵的一次误操作，这里必须拦住。"""
    out = tmp_path / "handwritten"
    out.mkdir()
    (out / "unsorted.md").write_text("# 手写的内容\n", encoding="utf-8")
    repository.create(RecordDraft(title="t", body="b"))

    result = sync_derived(config=_config(tmp_path, out), repository=repository)

    assert result["ok"] is False
    assert (out / "unsorted.md").read_text(encoding="utf-8") == "# 手写的内容\n"


def test_adopt_takes_over_hand_written_files_after_backing_them_up(
    tmp_path: Path, repository: RecordRepository
) -> None:
    """一次性交接：把人工维护的目录正式改由 engram 生成。

    接管是不可逆的覆盖，所以先留一份原件；没有备份的接管等于把多年笔记
    压在一次命令上。
    """
    out = tmp_path / "handover"
    out.mkdir()
    original = "# 手写的内容\n正文若干\n"
    (out / "unsorted.md").write_text(original, encoding="utf-8")
    repository.create(RecordDraft(title="新记录", body="接管之后写进来的"))

    report = export_markdown(repository=repository, out_dir=out, adopt=True)

    adopted = (out / "unsorted.md").read_text(encoding="utf-8")
    assert adopted.startswith(DERIVED_MARKER)
    assert "接管之后写进来的" in adopted
    backups = list(out.glob(".engram-backup-*/unsorted.md"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert report["backed_up"] == 1


def test_export_still_refuses_hand_written_files_without_adopt(
    tmp_path: Path, repository: RecordRepository
) -> None:
    out = tmp_path / "protected"
    out.mkdir()
    (out / "unsorted.md").write_text("# 手写的内容\n", encoding="utf-8")
    repository.create(RecordDraft(title="t", body="b"))

    with pytest.raises(InvalidInputError):
        export_markdown(repository=repository, out_dir=out)


# 两条写入路径都必须自动同步。同步只接在其中一条上，另一条就会悄悄
# 让备份落后于库——而这种偏差要等到需要备份的那天才会被发现。


def test_cli_write_syncs(tmp_path: Path, monkeypatch, capsys) -> None:
    out = tmp_path / "derived"
    monkeypatch.setenv("ENGRAM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ENGRAM_EXPORT_DIR", str(out))
    from engram.cli import main

    assert main(["record", "create", "--body", "命令行写进来的"]) == 0
    assert json.loads(capsys.readouterr().out)["sync"]["ok"] is True
    written = "".join(path.read_text(encoding="utf-8") for path in out.glob("*.md"))
    assert "命令行写进来的" in written


def test_mcp_write_syncs(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "derived"
    monkeypatch.setenv("ENGRAM_EXPORT_DIR", str(out))
    from engram.mcp.tools import ToolContext, call_tool

    context = ToolContext.open(data_dir=tmp_path / "data", offline=True)
    result = call_tool(context, "remember", {"body": "MCP 写进来的"})

    assert result["sync"]["ok"] is True
    written = "".join(path.read_text(encoding="utf-8") for path in out.glob("*.md"))
    assert "MCP 写进来的" in written
