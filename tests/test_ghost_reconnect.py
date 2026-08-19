"""幽灵连接自愈：库文件被替换时，长连接必须重连真实库。

2026-08-17 事故的另一半原因：恢复操作 rm+cp 替换了库文件，而前一天启动的
MCP server 持有被删除文件的 fd，之后的写入全部进入幽灵库——返回成功、
sync 正常，但永不落盘。进程回收后数据蒸发，且找不到任何删除者。

检测手段经隔离实验验证：库路径的 (st_dev, st_ino) 指纹在文件被替换时
必然变化，而正常 checkpoint 原位写入、inode 不变，因此无误报。
"""

import os
import shutil
from pathlib import Path

from engram.mcp.tools import ToolContext, call_tool


def _replace_db_file(target: Path, source: Path) -> None:
    """模拟恢复操作：删除目标库（含 WAL/SHM），把另一个库文件放上来。"""
    os.remove(target)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(target) + suffix)
        if sidecar.exists():
            os.remove(sidecar)
    shutil.copy(source, target)


def test_write_reconnects_when_db_file_is_replaced(tmp_path: Path) -> None:
    context = ToolContext.open(data_dir=tmp_path / "data", offline=True)
    call_tool(context, "remember", {"body": "替换前写入的记录"})

    # 另一个库作为"恢复快照"：内容不同、inode 必然不同
    other = ToolContext.open(data_dir=tmp_path / "other", offline=True)
    call_tool(other, "remember", {"body": "快照里的记录"})
    other.repository.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    snapshot = other.config.db_path
    other.repository.connection.close()

    _replace_db_file(context.config.db_path, snapshot)

    result = call_tool(context, "remember", {"body": "替换之后写入的记录"})

    assert result.get("reconnected") is True
    # 关键断言：写入落在真实库，而不是幽灵库
    fresh = ToolContext.open(data_dir=tmp_path / "data", offline=True)
    bodies = [
        hit["title"]
        for hit in call_tool(fresh, "recall", {"query": "记录", "top_k": 20})["results"]
    ]
    assert any("替换之后写入的记录" in b for b in bodies)
    assert any("快照里的记录" in b for b in bodies)
    assert not any("替换前写入的记录" in b for b in bodies)


def test_normal_write_does_not_report_reconnect(tmp_path: Path) -> None:
    context = ToolContext.open(data_dir=tmp_path / "data", offline=True)
    call_tool(context, "remember", {"body": "第一条"})

    result = call_tool(context, "remember", {"body": "第二条"})

    assert "reconnected" not in result
