"""stdio 上的 JSON-RPC 2.0 传输。

本地 stdio 保持零额外依赖；可选 HTTP/SSE 传输位于 remote.py，使用官方 SDK。

错误分两层，混淆这两层会让模型收不到该收的反馈：

- **传输错误**（JSON-RPC `error`）：协议层面出了问题，比如报文坏了、方法不存在。
  模型无从纠正。
- **工具错误**（`result.isError`）：参数写错、记录不存在。这是模型该看见并据此
  重试的信息，所以必须作为正常结果回给它，而不是让调用整个失败。
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from engram.errors import EngramError
from engram.mcp.profile import DEFAULT_PROFILE, MCPProfile, get_profile
from engram.mcp.tools import ToolContext, call_tool, tool_descriptors

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "engram"
SERVER_VERSION = "0.4.0"
INSTRUCTIONS = DEFAULT_PROFILE.instructions

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


def _result(id_: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _content(payload: dict, *, is_error: bool) -> dict:
    # 统一回 JSON 文本：结构化数据比散文更省 token，模型解析也更稳。
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "isError": is_error,
    }


def _initialize(profile: MCPProfile = DEFAULT_PROFILE) -> dict:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {
            "name": profile.server_name,
            "title": profile.title,
            "version": SERVER_VERSION,
        },
        "instructions": profile.instructions,
    }


def _call(context: ToolContext, params: dict) -> dict:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}
    try:
        return _content(call_tool(context, name, arguments), is_error=False)
    except EngramError as error:
        return _content(
            error.to_problem(instance=f"mcp:{name}").to_dict(), is_error=True
        )
    except Exception as error:  # noqa: BLE001 - 一次调用失败不该拖垮长连接会话
        return _content(
            {"code": "SB-500-INTERNAL", "detail": str(error)}, is_error=True
        )


def _dispatch(
    context: ToolContext, request: dict, profile: MCPProfile = DEFAULT_PROFILE
) -> dict | None:
    id_ = request.get("id")
    method = request.get("method")
    # 通知没有 id，按协议不能回。
    if id_ is None:
        return None
    if not isinstance(method, str):
        return _error(id_, INVALID_REQUEST, "method is required")
    params = request.get("params") or {}
    if method == "initialize":
        return _result(id_, _initialize(profile))
    if method == "tools/list":
        return _result(id_, {"tools": tool_descriptors()})
    if method == "tools/call":
        return _result(id_, _call(context, params))
    if method == "ping":
        return _result(id_, {})
    return _error(id_, METHOD_NOT_FOUND, f"unknown method: {method}")


def serve(
    context: ToolContext,
    *,
    profile: str = "default",
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """逐行读取请求直到输入结束。

    一行一条 JSON。任何单条请求的失败都只影响该条响应，会话继续——
    MCP 会话一旦断开，宿主通常不会自动重连，代价远大于一次调用出错。
    """
    selected = get_profile(profile)
    context.client_profile = selected.key
    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as error:
            response = _error(None, PARSE_ERROR, f"invalid JSON: {error}")
        else:
            if not isinstance(request, dict):
                response = _error(None, INVALID_REQUEST, "request must be an object")
            else:
                response = _dispatch(context, request, selected)
        if response is None:
            continue
        sink.write(json.dumps(response, ensure_ascii=False) + "\n")
        sink.flush()
