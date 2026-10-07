"""Bounded, model-free measurements of the production Engram store only."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from engram.repository import RecordRepository
from engram.status import collect_status

ZONE = ZoneInfo("Asia/Shanghai")


def _readonly(path: Path) -> sqlite3.Connection:
    # mode=ro never creates a missing database, migrates, drains or exports it.
    c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    deadline = monotonic() + 8
    c.set_progress_handler(lambda: int(monotonic() > deadline), 1000)
    return c


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _usage(path: Path, start: datetime, end: datetime) -> dict:
    if not path.is_file():
        return {"available": False, "reason": "usage_sidecar_missing"}
    c = None
    try:
        c = _readonly(path)
        rows = [
            dict(r)
            for r in c.execute(
                "SELECT day,tool,variant,calls,errors FROM usage_daily "
                "WHERE day >= ? AND day <= ? ORDER BY day,tool,variant",
                (start.date().isoformat(), end.date().isoformat()),
            )
        ]
        return {
            "available": True,
            "timezone": "UTC",
            "daily": rows,
            "calls": sum(r["calls"] for r in rows),
            "errors": sum(r["errors"] for r in rows),
            "non_status_calls": sum(r["calls"] for r in rows if r["tool"] != "status"),
            "note": "日粒度本机采样，含自动调用；缺行不证明无人使用，"
            "不代表完整客户端覆盖或模型效果。首尾为UTC整日，"
            "与记录新增的精确滚动窗口不同。",
        }
    except (OSError, sqlite3.Error):
        return {"available": False, "reason": "usage_sidecar_unreadable"}
    finally:
        if c is not None:
            c.close()


def health_snapshot(config, *, window_days: int = 14, now=None) -> dict:
    if type(window_days) is not int or not 1 <= window_days <= 31:
        raise ValueError("window_days must be an integer from 1 to 31")
    end = (now or datetime.now(UTC)).astimezone(UTC)
    start = end - timedelta(days=window_days)
    result = {
        "schema_version": 1,
        "scope": "engram",
        "read_only": True,
        "model_used": False,
        "observed_at": _utc(end),
        "window": {
            "days": window_days,
            "from_inclusive": _utc(start),
            "to_exclusive": _utc(end),
            "daily_timezone": "Asia/Shanghai",
        },
        "metrics": None,
        "integrity": {"state": "unknown"},
        "findings": [],
    }
    c = None
    try:
        c = _readonly(config.db_path)
        c.execute("BEGIN")
        summary = collect_status(
            repository=RecordRepository(c), data_dir=config.data_dir, now=end
        )
        summary.pop("data_dir", None)
        summary["stage2_ready"].pop("anchors_path", None)
        summary["active_records"] = c.execute(
            "SELECT COUNT(*) FROM records WHERE status='active'"
        ).fetchone()[0]
        summary["archived_records"] = summary["records"] - summary["active_records"]
        summary["active_without_vector"] = c.execute(
            "SELECT COUNT(*) FROM records r WHERE r.status='active' "
            "AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.record_id=r.record_id)"
        ).fetchone()[0]
        summary["active_islands"] = c.execute(
            "SELECT COUNT(*) FROM records r WHERE r.status='active' "
            "AND NOT EXISTS (SELECT 1 FROM record_links l "
            "WHERE l.source_id=r.record_id OR l.target_id=r.record_id)"
        ).fetchone()[0]
        tags = c.execute(
            "SELECT COUNT(*),COALESCE(SUM(n=1),0) FROM "
            "(SELECT COUNT(*) n FROM facets WHERE kind='tag' GROUP BY value)"
        ).fetchone()
        summary["tags"] = {"distinct": tags[0], "single_record": tags[1]}
        summary["links"] = {
            "total": c.execute("SELECT COUNT(*) FROM record_links").fetchone()[0]
        }
        summary["links"]["by_source"] = {
            r[0]: r[1]
            for r in c.execute(
                "SELECT provenance,COUNT(*) FROM record_links GROUP BY provenance"
            )
        }
        days = {}
        day = start.astimezone(ZONE).date()
        while day <= end.astimezone(ZONE).date():
            days[day.isoformat()] = 0
            day += timedelta(days=1)
        for row in c.execute(
            "SELECT date(created_at,'+8 hours'),COUNT(*) FROM records "
            "WHERE julianday(created_at)>=julianday(?) "
            "AND julianday(created_at)<julianday(?) GROUP BY 1",
            (_utc(start), _utc(end)),
        ):
            days[row[0]] = row[1]
        summary["daily_new_records"] = [{"day": k, "count": v} for k, v in days.items()]
        summary["new_in_window"] = sum(days.values())
        summary["new_last_4_days"] = c.execute(
            "SELECT COUNT(*) FROM records WHERE julianday(created_at)>=julianday(?) "
            "AND julianday(created_at)<julianday(?)",
            (_utc(end - timedelta(days=4)), _utc(end)),
        ).fetchone()[0]
        summary["invalid_created_at"] = c.execute(
            "SELECT COUNT(*) FROM records WHERE julianday(created_at) IS NULL"
        ).fetchone()[0]
        result["metrics"] = summary
        try:
            import sqlite_vec

            c.enable_load_extension(True)
            try:
                sqlite_vec.load(c)
            finally:
                c.enable_load_extension(False)
            checked = [r[0] for r in c.execute("PRAGMA integrity_check(10)")]
            # Do not return raw corrupt-page messages or any record content.
            result["integrity"] = {
                "state": "ok" if checked == ["ok"] else "error",
                "check": "integrity_check",
                "reported_issues": 0 if checked == ["ok"] else len(checked),
            }
        except (OSError, sqlite3.Error):
            result["integrity"] = {
                "state": "unknown",
                "reason": "integrity_check_failed",
            }
    except (OSError, sqlite3.Error):
        result["findings"].append({"code": "source_unavailable", "severity": "unknown"})
    finally:
        if c is not None:
            c.close()
    metrics = result["metrics"]
    if metrics is not None:
        for code, value, severity in [
            ("permanent_backlog", metrics["backlog"]["permanent"], "error"),
            ("pending_backlog", metrics["backlog"]["pending"], "warning"),
            ("missing_active_vectors", metrics["active_without_vector"], "warning"),
            ("active_islands", metrics["active_islands"], "warning"),
            ("invalid_created_at", metrics["invalid_created_at"], "unknown"),
            ("curation_due", int(metrics["curation_due"]["due"]), "info"),
        ]:
            if value:
                result["findings"].append(
                    {"code": code, "severity": severity, "count": value}
                )
    if result["integrity"]["state"] != "ok":
        result["findings"].append(
            {"code": "integrity", "severity": result["integrity"]["state"]}
        )
    result["usage"] = _usage(config.usage_db_path, start, end)
    if not result["usage"]["available"]:
        result["findings"].append({"code": "usage_unavailable", "severity": "unknown"})
    result["complete"] = not any(f["severity"] == "unknown" for f in result["findings"])
    result["notes"] = [
        "只采集正式Engram；不访问项目试点、影子库、旧Second Brain或私人会话。",
        "首次采样没有跨次变化基线；比较时使用上次同口径结果。",
        "标签、孤岛、积压与低调用量是核查信号，不是自动修复或内容失效结论。",
        "未运行召回评测，不能凭这些指标宣称检索质量提升。",
    ]
    return result
