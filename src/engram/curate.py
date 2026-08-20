"""curate：确定性整理执行器。

大模型（或人）负责裁决"该怎么整理"，本模块负责把裁决确定性地落库。
裁决与执行分离的原因：LLM 产出的是意图，意图必须经过校验、快照、
单事务执行和对账，才允许碰到真实数据。

约束：curate 永远只走 CLI——MCP 工具集固定为四个（test_mcp_tools.py
断言），整理是低频高影响操作，不该成为 Agent 随手可调的接口。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engram.db import write_transaction
from engram.errors import InvalidInputError

if TYPE_CHECKING:
    from engram.repository import RecordRepository

OP_NAMES = ("merge_tag", "delete_tag", "prune_links", "set_tag")
_FACET_KINDS = ("tag", "domain")


def _validate(ops: list[dict[str, Any]]) -> None:
    """全部 op 先校验再执行：任何一个非法，整批都不落库。"""
    for index, op in enumerate(ops):
        name = op.get("op")
        if name not in OP_NAMES:
            raise InvalidInputError(
                "unknown curate op", context={"index": index, "op": name}
            )
        if name == "merge_tag":
            source, target = op.get("from"), op.get("to")
            if not source or not target or source == target:
                raise InvalidInputError(
                    "merge_tag requires distinct from/to",
                    context={"index": index, "op": op},
                )
        elif name == "delete_tag":
            if not op.get("value"):
                raise InvalidInputError(
                    "delete_tag requires value", context={"index": index}
                )
        elif name == "prune_links":
            below = op.get("below")
            if not isinstance(below, int | float) or not 0 < below <= 1:
                raise InvalidInputError(
                    "prune_links requires 0 < below <= 1", context={"index": index}
                )
        elif name == "set_tag":
            if not op.get("record_id") or not op.get("value"):
                raise InvalidInputError(
                    "set_tag requires record_id and value", context={"index": index}
                )
            if op.get("kind", "tag") not in _FACET_KINDS:
                raise InvalidInputError(
                    "set_tag kind must be tag or domain", context={"index": index}
                )


def _affected_rows(connection: sqlite3.Connection, op: dict[str, Any]) -> list[dict]:
    """执行前取出受影响行，供快照回溯。"""
    name = op["op"]
    if name == "merge_tag":
        rows = connection.execute(
            "SELECT * FROM facets WHERE kind = 'tag' AND value IN (?, ?)",
            (op["from"], op["to"]),
        ).fetchall()
    elif name == "delete_tag":
        rows = connection.execute(
            "SELECT * FROM facets WHERE kind = 'tag' AND value = ?", (op["value"],)
        ).fetchall()
    elif name == "prune_links":
        rows = connection.execute(
            "SELECT * FROM record_links WHERE score < ? AND provenance = 'knn'",
            (op["below"],),
        ).fetchall()
    else:  # set_tag
        rows = connection.execute(
            "SELECT * FROM facets WHERE record_id = ? AND kind = ?",
            (op["record_id"], op.get("kind", "tag")),
        ).fetchall()
    return [dict(row) for row in rows]


def _execute(connection: sqlite3.Connection, op: dict[str, Any]) -> dict[str, Any]:
    name = op["op"]
    if name == "merge_tag":
        # facets 主键是 (record_id, kind, value)：裸 UPDATE 会在目标标签
        # 已存在的记录上撞 UNIQUE。先删掉会冲突的 from 行，再改名剩余行。
        conflicts = connection.execute(
            "DELETE FROM facets WHERE kind = 'tag' AND value = ? AND record_id IN "
            "(SELECT record_id FROM facets WHERE kind = 'tag' AND value = ?)",
            (op["from"], op["to"]),
        ).rowcount
        renamed = connection.execute(
            "UPDATE facets SET value = ?, provenance = 'human', confidence = 1.0 "
            "WHERE kind = 'tag' AND value = ?",
            (op["to"], op["from"]),
        ).rowcount
        return {"op": name, "renamed": renamed, "conflicts_dropped": conflicts}
    if name == "delete_tag":
        deleted = connection.execute(
            "DELETE FROM facets WHERE kind = 'tag' AND value = ?", (op["value"],)
        ).rowcount
        return {"op": name, "deleted": deleted}
    if name == "prune_links":
        # 人工边不按分数清：human provenance 是刻意裁决，不是噪音。
        deleted = connection.execute(
            "DELETE FROM record_links WHERE score < ? AND provenance = 'knn'",
            (op["below"],),
        ).rowcount
        return {"op": name, "deleted": deleted}
    # set_tag
    exists = connection.execute(
        "SELECT 1 FROM records WHERE record_id = ?", (op["record_id"],)
    ).fetchone()
    if exists is None:
        raise InvalidInputError(
            "set_tag target record not found",
            context={"record_id": op["record_id"]},
        )
    connection.execute(
        "INSERT INTO facets(record_id, kind, value, provenance, confidence, locked) "
        "VALUES (?, ?, ?, 'human', 1.0, 1) "
        "ON CONFLICT(record_id, kind, value) "
        "DO UPDATE SET provenance = 'human', confidence = 1.0, locked = 1",
        (op["record_id"], op.get("kind", "tag"), op["value"]),
    )
    return {"op": name, "set": 1}


def _snapshot(data_dir: Path, moment: datetime, rows: list[dict]) -> Path:
    backup_dir = data_dir / "curate-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    out = backup_dir / f"{moment.strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    with out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return out


def apply_ops(
    *,
    repository: RecordRepository,
    ops: list[dict[str, Any]],
    data_dir: Path,
    apply: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """校验 → （快照）→ 单事务执行 + meta 盖章 → 对账。

    dry-run（apply=False）只校验并报告会影响多少行，不写任何东西。
    """
    if not isinstance(ops, list):
        raise InvalidInputError("ops must be a list")
    _validate(ops)
    moment = now or datetime.now(UTC)
    connection = repository.connection

    links_before = connection.execute("SELECT COUNT(*) FROM record_links").fetchone()[0]
    tags_before = connection.execute(
        "SELECT COUNT(DISTINCT value) FROM facets WHERE kind = 'tag'"
    ).fetchone()[0]

    if not apply:
        plan = [
            {**op_report_hint(op), "affected": len(_affected_rows(connection, op))}
            for op in ops
        ]
        return {"applied": False, "plan": plan}

    affected = [row for op in ops for row in _affected_rows(connection, op)]
    snapshot = _snapshot(data_dir, moment, affected)

    reports = []
    with write_transaction(connection) as tx:
        for op in ops:
            reports.append(_execute(tx, op))
        tx.execute(
            "INSERT INTO meta(key, value) VALUES ('last_curate_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (moment.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        )
        tx.execute(
            "INSERT INTO meta(key, value) VALUES ('last_curate_count', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(repository.count()),),
        )

    return {
        "applied": True,
        "reports": reports,
        "snapshot": str(snapshot),
        "reconciliation": {
            "links": {
                "before": links_before,
                "after": connection.execute(
                    "SELECT COUNT(*) FROM record_links"
                ).fetchone()[0],
            },
            "distinct_tags": {
                "before": tags_before,
                "after": connection.execute(
                    "SELECT COUNT(DISTINCT value) FROM facets WHERE kind = 'tag'"
                ).fetchone()[0],
            },
            "records": repository.count(),
        },
    }


def op_report_hint(op: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in op.items()}
