"""MCP stdio 传输层。

传输自行实现而不引入官方 SDK：本项目只用 stdio，而 SDK 的体量几乎都在
HTTP、OAuth 与遥测上——那与"完全本地"相悖，也让别人 clone 之后无法开箱即跑。
代价是协议演进要自己跟，因此这里把握手、方法分发和错误映射逐条钉死。
"""

import io
import json
from pathlib import Path

import pytest

from engram.mcp.server import PROTOCOL_VERSION, serve
from engram.mcp.tools import ToolContext


def _exchange(context: ToolContext, *requests: dict) -> list[dict]:
    """把若干请求喂给 server，收回响应。"""
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()
    serve(context, stdin=stdin, stdout=stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]


@pytest.fixture
def context(tmp_path: Path) -> ToolContext:
    return ToolContext.open(data_dir=tmp_path / "data", offline=True)


def _request(id_: int, method: str, params: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        payload["params"] = params
    return payload


def test_initialize_returns_protocol_and_server_info(context: ToolContext) -> None:
    (response,) = _exchange(context, _request(1, "initialize", {}))
    assert response["id"] == 1
    result = response["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "engram"
    assert "tools" in result["capabilities"]


def test_notifications_get_no_response(context: ToolContext) -> None:
    """通知没有 id，回它就是协议错误。"""
    assert _exchange(context, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_tools_list_returns_descriptors(context: ToolContext) -> None:
    (response,) = _exchange(context, _request(2, "tools/list"))
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names == {"remember", "recall", "get", "status"}


def test_tools_call_wraps_result_as_text_content(context: ToolContext) -> None:
    (response,) = _exchange(
        context,
        _request(3, "tools/call", {"name": "remember", "arguments": {"body": "一条记录"}}),
    )
    result = response["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["record_id"]


def test_tool_failure_is_reported_in_result_not_as_transport_error(
    context: ToolContext,
) -> None:
    """工具出错是模型该看见并纠正的信息，不是传输层故障。"""
    (response,) = _exchange(
        context,
        _request(4, "tools/call", {"name": "remember", "arguments": {}}),
    )
    assert "error" not in response
    result = response["result"]
    assert result["isError"] is True
    problem = json.loads(result["content"][0]["text"])
    assert problem["code"] == "SB-400-INVALID-INPUT"


def test_unknown_method_is_a_transport_error(context: ToolContext) -> None:
    (response,) = _exchange(context, _request(5, "resources/list"))
    assert response["error"]["code"] == -32601


def test_malformed_json_is_reported_and_does_not_kill_the_server(
    context: ToolContext,
) -> None:
    stdin = io.StringIO("{not json\n" + json.dumps(_request(6, "tools/list")) + "\n")
    stdout = io.StringIO()
    serve(context, stdin=stdin, stdout=stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["id"] == 6


def test_session_survives_a_failing_call(context: ToolContext) -> None:
    responses = _exchange(
        context,
        _request(7, "tools/call", {"name": "get", "arguments": {"record_id": "nope"}}),
        _request(8, "tools/call", {"name": "status", "arguments": {}}),
    )
    assert responses[0]["result"]["isError"] is True
    assert json.loads(responses[1]["result"]["content"][0]["text"])["records"] == 0
