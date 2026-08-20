"""status 出口共享的状态装配。

CLI 与 MCP 两个出口都从装配同一份 payload，避免两份实现悄悄漂移。
本模块刻意不引入向量/嵌入栈：写入路径的依赖面必须保持最小
（test_phase_acceptance.py 断言纯写入不加载嵌入模块）。

curation_due 与 stage2_ready 是状态机而不是日历提醒：每次 status 都
重新计算，任何接入的 Agent 都能看到，不依赖谁记得定期检查——机器
不在线时什么提醒都不会丢，上线后第一次 status 就会显示真实状态。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from engram.repository import RecordRepository

CURATE_NEW_RECORDS_THRESHOLD = 20
CURATE_MAX_AGE = timedelta(days=7)
STAGE2_MIN_RECORDS = 1000
STAGE2_ANCHORS_FILE = "eval-anchors.jsonl"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def collect_status(
    *,
    repository: RecordRepository,
    data_dir: Path,
    now: datetime | None = None,
) -> dict[str, object]:
    moment = now or datetime.now(UTC)
    connection = repository.connection
    total = repository.count()
    return {
        "records": total,
        "backlog": repository.backlog(),
        "vectors": connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0],
        "data_dir": str(data_dir),
        "curation_due": _curation_due(connection, total=total, moment=moment),
        "stage2_ready": _stage2_ready(data_dir=data_dir, total=total),
    }


def _curation_due(
    connection, *, total: int, moment: datetime
) -> dict[str, object]:
    rows = dict(connection.execute("SELECT key, value FROM meta").fetchall())
    last_at = rows.get("last_curate_at")
    last_count = rows.get("last_curate_count")
    if last_at is None or last_count is None:
        # 没有整理记录时 fail 向动作：宁可多提示一次，不可漏提示。
        return {
            "due": True,
            "reason": "never_curated",
            "new_records": total,
            "days_since": None,
        }
    new_records = total - int(last_count)
    elapsed = moment - datetime.strptime(last_at, _TIME_FORMAT).replace(tzinfo=UTC)
    if new_records >= CURATE_NEW_RECORDS_THRESHOLD:
        due, reason = True, "new_records"
    elif elapsed >= CURATE_MAX_AGE:
        due, reason = True, "age"
    else:
        due, reason = False, "none"
    return {
        "due": due,
        "reason": reason,
        "new_records": new_records,
        "days_since": elapsed.days,
    }


def _stage2_ready(*, data_dir: Path, total: int) -> dict[str, object]:
    anchors = data_dir / STAGE2_ANCHORS_FILE
    present = anchors.is_file()
    return {
        "ready": total >= STAGE2_MIN_RECORDS and present,
        "records": total,
        "min_records": STAGE2_MIN_RECORDS,
        "anchors_path": str(anchors),
        "anchors_present": present,
    }
