"""写入之后把库回写成 Markdown。

事实源换成 SQLite 之后，纯文本备份就不能再依赖"记得去导一次"。同步做在库
内部，而不是交给宿主的 Hook 或另一个工具：调用方只面对一个接口，CLI 与 MCP
两条路径也就不会各自漂移。

这里唯一的硬约束是**同步不得否决写入**。备份是可选功能，写入是这个库的底线，
让前者有能力让后者失败就是本末倒置——所以失败在这里被降级成一条可见的报告。
"""

from __future__ import annotations

from engram.config import EngramConfig
from engram.repository import RecordRepository


def sync_derived(
    *, config: EngramConfig, repository: RecordRepository
) -> dict[str, object] | None:
    """把当前库导出到 `export_dir`。未配置时返回 None，失败时返回原因。"""
    if config.export_dir is None and config.index_path is None:
        return None

    # 延迟导入：没开同步的进程不必为此加载导出模块。
    from engram.export import export_index, export_markdown

    result: dict[str, object] = {"ok": True}
    if config.export_dir is not None:
        try:
            report = export_markdown(repository=repository, out_dir=config.export_dir)
        except Exception as error:  # noqa: BLE001 - 任何失败都不允许传播到写入路径
            # 不静默吞掉：调用方拿到的是"写入成功、同步失败"，
            # 而不是一个看起来一切正常、实际备份早已停摆的库。
            result = {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
                "path": str(config.export_dir),
            }
        else:
            result = {"ok": True, "path": str(config.export_dir), **report}

    if config.index_path is not None:
        # 索引单独成败：它常驻上下文，过时的地图比没有更糟，
        # 但它出问题也不该连累全文备份的结论。
        try:
            index = export_index(repository=repository, out_file=config.index_path)
        except Exception as error:  # noqa: BLE001
            result["index"] = {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
                "path": str(config.index_path),
            }
        else:
            result["index"] = {"ok": True, "path": str(config.index_path), **index}
    return result
