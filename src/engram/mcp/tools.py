"""暴露给 Agent 的工具集。

工具只有四个，而且刻意不提供"选分类""改标签"这类动作：分类由系统统一裁决，
外部 Agent 可以触发但不可以裁决。少一个决策点，就少一处随平台漂移的地方。

`remember` 与 `recall` 的默认值都偏向"不需要模型也能用"——本机模型没起来时
写入照常、关键词检索照常，语义层由后续补全跟上。工具描述里必须把这件事讲清楚，
否则 Agent 会误以为刚写的内容立刻就能被语义召回。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engram.config import load_config
from engram.db import connect
from engram.domain import RecordDraft
from engram.errors import InvalidInputError
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.search import SearchService
from engram.sync import sync_derived

MAX_TOP_K = 20
_MODES = ("keyword", "vector", "hybrid")


@dataclass(slots=True)
class ToolContext:
    """一次会话共用的连接与配置。

    MCP 是长连接进程，数据库连接跟着会话走而不是跟着调用走；向量组件则相反，
    只在真正需要语义检索时才构建——这样"本机没有模型"就只影响语义模式，
    影响不到写入与关键词检索。
    """

    config: Any
    repository: RecordRepository
    search: SearchService
    offline: bool = False

    @classmethod
    def open(cls, *, data_dir: Path | str | None = None, offline: bool = False) -> ToolContext:
        config = load_config(data_dir=str(data_dir) if data_dir is not None else None)
        connection = connect(config.db_path)
        migrate(connection)
        return cls(
            config=config,
            repository=RecordRepository(connection),
            search=SearchService(connection),
            offline=offline,
        )

    def _vector(self):
        from engram.embedding import DeterministicEmbedder, OllamaEmbedder
        from engram.vectors import VectorStore

        embedder = (
            DeterministicEmbedder(dimensions=64)
            if self.offline
            else OllamaEmbedder(
                model=self.config.embedding_model,
                dimensions=self.config.embedding_dimensions,
                base_url=self.config.ollama_base_url,
            )
        )
        self.search.store = VectorStore(
            self.repository.connection, dimensions=embedder.dimensions
        )
        return embedder


def _require_text(arguments: dict, name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidInputError(
            f"{name} is required and must be non-empty",
            context={"argument": name},
        )
    return value


def _remember(context: ToolContext, arguments: dict) -> dict[str, object]:
    body = _require_text(arguments, "body")
    projects = arguments.get("projects") or []
    if not isinstance(projects, list):
        raise InvalidInputError(
            "projects must be a list of strings", context={"argument": "projects"}
        )
    record = context.repository.create(
        RecordDraft(
            title=arguments.get("title") or "",
            body=body,
            record_type=arguments.get("type") or "note",
            projects=tuple(str(project) for project in projects),
            source_agent=arguments.get("agent") or "mcp",
        )
    )
    payload = record.to_dict()
    # 积压一并返回：写入是即时的、语义补全是异步的，不讲清楚
    # 调用方会以为刚写的内容立刻就能被语义召回。
    payload["backlog"] = context.repository.backlog()
    sync = sync_derived(config=context.config, repository=context.repository)
    if sync is not None:
        payload["sync"] = sync
    return payload


def _recall(context: ToolContext, arguments: dict) -> dict[str, object]:
    query = _require_text(arguments, "query")
    mode = arguments.get("mode") or "keyword"
    if mode not in _MODES:
        raise InvalidInputError(
            "unknown search mode",
            context={"mode": mode, "supported": list(_MODES)},
        )
    top_k = arguments.get("top_k") or 5
    if not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
        # 无上限的 top_k 会让一次调用吃掉整个上下文窗口。
        raise InvalidInputError(
            "top_k out of range",
            context={"top_k": top_k, "max": MAX_TOP_K},
        )

    if mode == "keyword":
        hits = context.search.keyword(query, limit=top_k)
    else:
        embedder = context._vector()
        vector = embedder.embed([query])[0]
        hits = (
            context.search.vector(vector, limit=top_k)
            if mode == "vector"
            else context.search.hybrid(query, vector, limit=top_k)
        )
    return {"mode": mode, "results": [hit.to_dict() for hit in hits]}


def _get(context: ToolContext, arguments: dict) -> dict[str, object]:
    return context.repository.get(_require_text(arguments, "record_id")).to_dict()


def _status(context: ToolContext, arguments: dict) -> dict[str, object]:
    connection = context.repository.connection
    return {
        "records": context.repository.count(),
        "backlog": context.repository.backlog(),
        "vectors": connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        "data_dir": str(context.config.data_dir),
    }


TOOLS = {
    "remember": {
        "handler": _remember,
        "description": (
            "把一条内容写入知识库。不需要判断分类或放在哪里——分类由系统统一裁决。"
            "写入立即完成且不依赖本机模型；语义检索需要等后台补全，返回的 backlog "
            "就是待补全的数量。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "正文，必填"},
                "title": {"type": "string", "description": "标题，留空则由正文推断"},
                "projects": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "所属项目，可多个",
                },
                "type": {
                    "type": "string",
                    "description": "记录类型，默认 note",
                },
                "agent": {
                    "type": "string",
                    "description": "写入方标识，便于回溯来源",
                },
            },
            "required": ["body"],
        },
    },
    "recall": {
        "handler": _recall,
        "description": (
            "检索知识库，返回标题与摘要。keyword 不依赖模型、随时可用；"
            "vector 与 hybrid 需要本机嵌入模型。要看全文用 get。"
        ),
        "schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "查询词，必填"},
                "mode": {
                    "type": "string",
                    "enum": list(_MODES),
                    "description": "检索模式，默认 keyword",
                },
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_TOP_K,
                    "description": f"返回条数，1-{MAX_TOP_K}，默认 5",
                },
            },
            "required": ["query"],
        },
    },
    "get": {
        "handler": _get,
        "description": "按 record_id 取一条记录的完整正文与属性。",
        "schema": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string", "description": "recall 返回的 record_id"}
            },
            "required": ["record_id"],
        },
    },
    "status": {
        "handler": _status,
        "description": "查看记录总数、待补全积压与向量数量，用于判断语义检索是否已就绪。",
        "schema": {"type": "object", "properties": {}},
    },
}


def tool_descriptors() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "description": tool["description"],
            "inputSchema": tool["schema"],
        }
        for name, tool in TOOLS.items()
    ]


def call_tool(context: ToolContext, name: str, arguments: dict) -> dict[str, object]:
    tool = TOOLS.get(name)
    if tool is None:
        raise InvalidInputError(
            "unknown tool",
            context={"tool": name, "supported": sorted(TOOLS)},
        )
    return tool["handler"](context, arguments or {})
