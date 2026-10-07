"""MCP 工具层。

工具是 Agent 唯一能看见的接口，因此这里测的是**契约**：工具名、参数
schema、返回结构，以及"写入不依赖模型"这条底线在 MCP 路径上同样成立。

传输层单独测（见 test_mcp_server.py），这里只测纯粹的调用结果。
"""

import sqlite3
from pathlib import Path

import pytest

from engram.domain import RecordDraft
from engram.errors import InvalidInputError, RecordNotFoundError
from engram.mcp.tools import TOOLS, ToolContext, call_tool, tool_descriptors


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    return ToolContext.open(data_dir=tmp_path / "data", offline=True)


def test_exposes_exactly_the_agreed_tools() -> None:
    assert set(TOOLS) == {
        "feedback",
        "remember",
        "recall",
        "get",
        "status",
        "maintain",
        "run_receipt",
        "maintenance_candidates",
        "inspect_record",
        "source_improvement",
        "engram_maintenance_status",
        "engram_maintenance_catalog",
        "engram_maintenance_read",
    }
    assert {x["name"] for x in tool_descriptors()} == {
        "feedback",
        "remember",
        "recall",
        "get",
        "status",
        "inspect_record",
        "engram_maintenance_status",
        "engram_maintenance_catalog",
        "engram_maintenance_read",
    }


def test_every_descriptor_is_self_describing() -> None:
    """Agent 靠描述决定调不调用，缺一项就等于这个工具不存在。"""
    for descriptor in tool_descriptors():
        assert descriptor["name"]
        assert descriptor["description"].strip()
        schema = descriptor["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema


def test_remember_returns_record_id_and_backlog(context: ToolContext) -> None:
    result = call_tool(context, "remember", {"body": "synthetic-prism channel=amber"})
    assert result["record_id"]
    # 积压要回给调用方：写入是即时的，语义补全是后续的，
    # 不告诉 Agent 就等于让它以为已经可被语义检索。
    assert "backlog" in result


def test_feedback_submission_and_runtime_status(context: ToolContext) -> None:
    item = call_tool(
        context,
        "feedback",
        {
            "action": "add",
            "summary": "反馈收件箱应保留问题",
            "category": "quality",
        },
    )
    assert item["status"] == "open"
    report = call_tool(context, "status", {"detail": "feedback"})
    assert report["feedback"]["open"] == 1
    assert report["events"]["calls"] >= 1


def test_invalid_remember_records_reason_without_input(context: ToolContext):
    from datetime import UTC, datetime, timedelta

    from engram.feedback import FeedbackStore

    with pytest.raises(InvalidInputError):
        call_tool(context, "remember", {"body": ["private-body"]})
    report = FeedbackStore(context.config.feedback_db_path).snapshot(now=datetime.now(UTC) + timedelta(seconds=1))
    assert report["recent_errors"][0]["diagnostic"] == {
        "code": "invalid_type", "field": "body", "input_type": "list"
    }
    assert "private-body" not in str(report)


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


def test_remember_advertises_the_valid_types(context: ToolContext) -> None:
    """不声明合法值，调用方只能猜——而猜错的代价是整条内容写不进来。"""
    from engram.domain import RECORD_TYPES

    assert set(TOOLS["remember"]["schema"]["properties"]["type"]["enum"]) == set(
        RECORD_TYPES
    )


def test_an_unknown_type_degrades_instead_of_rejecting(context: ToolContext) -> None:
    """类型猜错不该让写入失败。

    合成反例：标签字符串 `project-status` 不能使正文写入失败。
    写入永不失败的优先级高于类型严格——类型是元数据，正文才是要保住的东西。
    """
    result = call_tool(
        context, "remember", {"body": "类型猜错也要能写进来", "type": "project-status"}
    )

    assert result["record_type"] == "note"


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


def test_semantic_search_drains_the_backlog(context: ToolContext) -> None:
    """语义检索顺带补全积压。

    写入不依赖模型，代价是语义层要靠后续调用补上。没有这一步，新写的内容
    在有人想起来手动 drain 之前都召不回来——而调用方并不知道该去 drain。
    """
    call_tool(context, "remember", {"body": "顺带补全的验证"})
    assert context.repository.backlog()["pending"] == 1

    result = call_tool(context, "recall", {"query": "补全", "mode": "hybrid"})

    assert context.repository.backlog()["pending"] == 0
    assert result["backfilled"]["succeeded"] == 1


def test_keyword_search_never_touches_the_model(context: ToolContext) -> None:
    """keyword 保持零模型依赖——这是它随时可用的全部理由。

    顺带补全只挂在本来就要加载模型的路径上，绝不能让关键词检索也背上这个代价。
    """
    call_tool(context, "remember", {"body": "关键词路径不补全"})

    result = call_tool(context, "recall", {"query": "关键词"})

    assert "backfilled" not in result
    assert context.repository.backlog()["pending"] == 1


def test_a_failing_backfill_still_returns_results(
    context: ToolContext, monkeypatch
) -> None:
    """补全是顺带的：它出问题不该让检索失败。"""
    from engram import enrich

    def explode(self, **kwargs):
        raise RuntimeError("model died")

    monkeypatch.setattr(enrich.EnrichmentService, "drain", explode)
    call_tool(context, "remember", {"body": "补全失败也要能查"})

    result = call_tool(context, "recall", {"query": "补全失败", "mode": "hybrid"})

    assert result["results"]
    assert result["backfilled"]["error"]


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


def test_status_reports_evolution_triggers(context: ToolContext) -> None:
    """curation_due / stage2_ready 是状态机：每个 Agent 调 status 都该看到。"""
    status = call_tool(context, "status", {})
    assert status["curation_due"]["due"] is True  # 从未整理过
    assert status["stage2_ready"]["ready"] is False
    assert "anchors_path" in status["stage2_ready"]


def test_unknown_tool_is_rejected(context: ToolContext) -> None:
    with pytest.raises(InvalidInputError):
        call_tool(context, "summon", {})

    assert not context.config.usage_db_path.exists()


def test_successful_call_aggregates_usage_and_runs_one_daily_tick(
    context: ToolContext,
) -> None:
    first = call_tool(context, "status", {})
    second = call_tool(context, "status", {})

    assert first["maintenance_warning"]["issues"] == [
        {
            "code": "curation_due",
            "reason": "never_curated",
            "new_records": 0,
        }
    ]
    assert "maintenance_warning" not in second
    connection = sqlite3.connect(context.config.usage_db_path)
    connection.row_factory = sqlite3.Row
    try:
        usage = connection.execute("SELECT * FROM usage_daily").fetchone()
        maintenance = connection.execute(
            "SELECT state, records, vectors FROM maintenance_daily"
        ).fetchall()
    finally:
        connection.close()
    assert usage["tool"] == "status"
    assert usage["variant"] == "default"
    assert usage["calls"] == 2
    assert usage["errors"] == 0
    assert [tuple(row) for row in maintenance] == [("warning", 0, 0)]


def test_failed_call_is_aggregated_without_claiming_daily_tick(
    context: ToolContext,
) -> None:
    with pytest.raises(InvalidInputError):
        call_tool(context, "remember", {"title": "missing body"})

    connection = sqlite3.connect(context.config.usage_db_path)
    try:
        usage = connection.execute(
            "SELECT calls, errors FROM usage_daily WHERE tool='remember'"
        ).fetchone()
        maintenance_count = connection.execute(
            "SELECT COUNT(*) FROM maintenance_daily"
        ).fetchone()[0]
    finally:
        connection.close()
    assert usage == (1, 1)
    assert maintenance_count == 0

    assert "maintenance_warning" in call_tool(context, "status", {})


def test_sidecar_failure_never_changes_the_main_result(
    context: ToolContext, monkeypatch
) -> None:
    from engram import maintenance

    def explode(*args, **kwargs):
        raise RuntimeError("sidecar unavailable")

    monkeypatch.setattr(maintenance.UsageSidecar, "record_and_check", explode)

    result = call_tool(context, "status", {})

    assert result["records"] == 0
    assert "maintenance_warning" not in result


def test_failed_daily_snapshot_is_retried_on_the_next_successful_call(
    context: ToolContext, monkeypatch
) -> None:
    from engram.maintenance import UsageSidecar

    original = UsageSidecar.finish_daily
    attempts = 0

    def fail_once(self, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return original(self, **kwargs)

    monkeypatch.setattr(UsageSidecar, "finish_daily", fail_once)

    first = call_tool(context, "status", {})
    second = call_tool(context, "status", {})

    assert "maintenance_warning" not in first
    assert "maintenance_warning" in second
    assert attempts == 2


def test_daily_tick_only_reads_authoritative_state(context: ToolContext) -> None:
    before = {
        "meta": context.repository.connection.execute(
            "SELECT COUNT(*) FROM meta"
        ).fetchone()[0],
        "outbox": context.repository.connection.execute(
            "SELECT COUNT(*) FROM outbox_jobs"
        ).fetchone()[0],
        "facets": context.repository.connection.execute(
            "SELECT COUNT(*) FROM facets"
        ).fetchone()[0],
        "links": context.repository.connection.execute(
            "SELECT COUNT(*) FROM record_links"
        ).fetchone()[0],
        "embeddings": context.repository.connection.execute(
            "SELECT COUNT(*) FROM embeddings"
        ).fetchone()[0],
    }

    call_tool(context, "status", {})

    after = {
        "meta": context.repository.connection.execute(
            "SELECT COUNT(*) FROM meta"
        ).fetchone()[0],
        "outbox": context.repository.connection.execute(
            "SELECT COUNT(*) FROM outbox_jobs"
        ).fetchone()[0],
        "facets": context.repository.connection.execute(
            "SELECT COUNT(*) FROM facets"
        ).fetchone()[0],
        "links": context.repository.connection.execute(
            "SELECT COUNT(*) FROM record_links"
        ).fetchone()[0],
        "embeddings": context.repository.connection.execute(
            "SELECT COUNT(*) FROM embeddings"
        ).fetchone()[0],
    }
    assert after == before


def test_daily_tick_reports_missing_vectors_without_backlog(
    context: ToolContext,
) -> None:
    context.repository.create(RecordDraft(title="t", body="b"))
    context.repository.connection.execute("DELETE FROM outbox_jobs")

    result = call_tool(context, "status", {})

    codes = [issue["code"] for issue in result["maintenance_warning"]["issues"]]
    assert codes == ["missing_vectors_without_backlog", "curation_due"]


def test_source_gateway_requires_both_runtime_permissions(context, monkeypatch):
    monkeypatch.delenv("ENGRAM_CLOUD_SOURCE_MAINTENANCE", raising=False)
    assert "source_improvement" not in {x["name"] for x in tool_descriptors()}
    with pytest.raises(InvalidInputError):
        call_tool(context, "source_improvement", {"action": "status"})
    monkeypatch.setenv("ENGRAM_CLOUD_SOURCE_MAINTENANCE", "1")
    monkeypatch.delenv("ENGRAM_AUTONOMOUS_MAINTENANCE", raising=False)
    assert "source_improvement" not in {x["name"] for x in tool_descriptors()}
    with pytest.raises(InvalidInputError):
        call_tool(context, "source_improvement", {"action": "status"})
    monkeypatch.setenv("ENGRAM_AUTONOMOUS_MAINTENANCE", "1")
    assert "source_improvement" in {x["name"] for x in tool_descriptors()}
