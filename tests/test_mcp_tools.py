"""MCP 工具层。

工具是 Agent 唯一能看见的接口，因此这里测的是**契约**：工具名、参数
schema、返回结构，以及"写入不依赖模型"这条底线在 MCP 路径上同样成立。

传输层单独测（见 test_mcp_server.py），这里只测纯粹的调用结果。
"""

from pathlib import Path

import pytest

from engram.errors import InvalidInputError, RecordNotFoundError
from engram.mcp.tools import TOOLS, ToolContext, call_tool, tool_descriptors


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    return ToolContext.open(data_dir=tmp_path / "data", offline=True)


def test_exposes_exactly_the_agreed_tools() -> None:
    assert set(TOOLS) == {"remember", "recall", "get", "status"}


def test_every_descriptor_is_self_describing() -> None:
    """Agent 靠描述决定调不调用，缺一项就等于这个工具不存在。"""
    for descriptor in tool_descriptors():
        assert descriptor["name"]
        assert descriptor["description"].strip()
        schema = descriptor["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema


def test_remember_returns_record_id_and_backlog(context: ToolContext) -> None:
    result = call_tool(context, "remember", {"body": "折叠支架的限位结构"})
    assert result["record_id"]
    # 积压要回给调用方：写入是即时的，语义补全是后续的，
    # 不告诉 Agent 就等于让它以为已经可被语义检索。
    assert "backlog" in result


def test_remember_accepts_optional_context(context: ToolContext) -> None:
    result = call_tool(
        context,
        "remember",
        {
            "body": "正文",
            "title": "标题",
            "projects": ["engram"],
            "type": "note",
            "agent": "claude-code",
        },
    )
    assert result["title"] == "标题"
    assert result["projects"] == ["engram"]
    assert result["source_agent"] == "claude-code"


def test_remember_requires_a_body(context: ToolContext) -> None:
    with pytest.raises(InvalidInputError):
        call_tool(context, "remember", {"title": "只有标题"})


def test_remember_rejects_an_empty_body(context: ToolContext) -> None:
    """空正文写进去就是一条永远召不回的垃圾记录。"""
    with pytest.raises(InvalidInputError):
        call_tool(context, "remember", {"body": "   "})


def test_remember_works_without_any_model(context: ToolContext) -> None:
    """写入永不失败：本机没有模型时，MCP 写入路径同样必须通。"""
    result = call_tool(context, "remember", {"body": "模型没启动也要能写"})
    assert result["record_id"]


def test_mcp_write_path_loads_no_model_modules(tmp_path: Path) -> None:
    """依赖层保证：走 MCP 写入同样不得把嵌入栈拖进来。

    在干净子进程里验证——`sys.modules` 是全局的，同进程内断言会被其他用例污染。
    """
    import subprocess
    import sys

    script = (
        "import sys;"
        "from engram.mcp.tools import ToolContext, call_tool;"
        f"ctx=ToolContext.open(data_dir={str(tmp_path / 'data')!r}, offline=True);"
        "call_tool(ctx,'remember',{'body':'x'});"
        "risky=[m for m in sys.modules "
        "if 'ollama' in m.lower() or m.endswith(('engram.embedding','engram.vectors')) "
        "or m in ('urllib.request','socket')];"
        "print(risky)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"


def test_recall_finds_what_was_remembered(context: ToolContext) -> None:
    call_tool(context, "remember", {"body": "沙盘推演用于压力测试", "title": "沙盘推演"})
    hits = call_tool(context, "recall", {"query": "沙盘推演"})["results"]
    assert hits
    assert "沙盘推演" in hits[0]["title"]


def test_recall_defaults_to_keyword_so_it_never_needs_a_model(
    context: ToolContext,
) -> None:
    call_tool(context, "remember", {"body": "关键词兜底"})
    assert call_tool(context, "recall", {"query": "关键词兜底"})["mode"] == "keyword"


def test_recall_rejects_an_unknown_mode(context: ToolContext) -> None:
    with pytest.raises(InvalidInputError):
        call_tool(context, "recall", {"query": "x", "mode": "telepathy"})


def test_recall_caps_top_k(context: ToolContext) -> None:
    """无上限的 top_k 会让一次调用吃掉整个上下文窗口。"""
    with pytest.raises(InvalidInputError):
        call_tool(context, "recall", {"query": "x", "top_k": 500})


def test_get_returns_the_full_body(context: ToolContext) -> None:
    body = "很长的正文" * 40
    record_id = call_tool(context, "remember", {"body": body})["record_id"]
    assert call_tool(context, "get", {"record_id": record_id})["body"] == body


def test_get_reports_a_missing_record(context: ToolContext) -> None:
    with pytest.raises(RecordNotFoundError):
        call_tool(context, "get", {"record_id": "does-not-exist"})


def test_status_reports_counts(context: ToolContext) -> None:
    call_tool(context, "remember", {"body": "一条"})
    status = call_tool(context, "status", {})
    assert status["records"] == 1
    assert "backlog" in status
    assert "vectors" in status


def test_unknown_tool_is_rejected(context: ToolContext) -> None:
    with pytest.raises(InvalidInputError):
        call_tool(context, "summon", {})
