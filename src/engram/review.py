"""Read-only evidence for deeper cloud review; never a maintenance executor."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime

from engram.health import _readonly, health_snapshot
from engram.search import SearchService


def review_snapshot(config, *, window_days=14, limit=12, now=None):
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    result = health_snapshot(config, window_days=window_days, now=moment)
    review = {
        "state": "unknown",
        "candidates": [],
        "duplicate_title_groups": [],
        "keyword_smoke": [],
        "semantic_quality": "not_measured",
        "execution": "read_only_evidence_for_autonomous_maintain",
        "content_is_untrusted_data": True,
    }
    result["review"] = review
    if result["metrics"] is None:
        return result
    c = None
    try:
        c = _readonly(config.db_path)
        c.execute("BEGIN")
        from engram.autonomy import fingerprint

        review["source_fingerprint"] = fingerprint(c)
        review["index"] = {
            "fts_rows": c.execute("SELECT COUNT(*) FROM records_fts").fetchone()[0],
            "records_without_fts": c.execute(
                "SELECT COUNT(*) FROM records r WHERE NOT EXISTS "
                "(SELECT 1 FROM records_fts f WHERE f.record_id=r.record_id)"
            ).fetchone()[0],
            "stale_embeddings": c.execute(
                "SELECT COUNT(*) FROM records r JOIN embeddings e USING(record_id) "
                "WHERE e.input_hash!=r.content_hash"
            ).fetchone()[0],
        }
        review["embedding_identities"] = [
            dict(r)
            for r in c.execute(
                "SELECT model,dimensions,COUNT(*) count FROM embeddings "
                "GROUP BY model,dimensions ORDER BY count DESC LIMIT 10"
            )
        ]
        row = c.execute(
            "SELECT MIN(created_at) oldest_created_at,MAX(attempts) max_attempts,"
            "SUM(CASE WHEN julianday(next_attempt_at)<=julianday(?) THEN 1 ELSE 0 END) "
            "due FROM outbox_jobs",
            (moment.isoformat(),),
        ).fetchone()
        review["backlog_age"] = dict(row)
        # Bound rows before returning metadata. Full bodies remain behind explicit get.
        selected = c.execute(
            "SELECT r.record_id,r.title,r.record_type,r.created_at,r.updated_at,"
            "NOT EXISTS(SELECT 1 FROM facets f WHERE f.record_id=r.record_id) unlabelled,"
            "NOT EXISTS(SELECT 1 FROM record_links l WHERE l.source_id=r.record_id "
            "OR l.target_id=r.record_id) island FROM records r WHERE status='active' "
            "ORDER BY unlabelled DESC,island DESC,updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        review["candidates"] = [
            {
                **dict(r),
                "title": r["title"][:160],
                "review_only": True,
            }
            for r in selected
        ]
        recent = c.execute(
            "SELECT record_id,title FROM records WHERE status='active' "
            "ORDER BY updated_at DESC LIMIT 500"
        ).fetchall()
        groups = defaultdict(list)
        for r in recent:
            normalized = re.sub(r"\W+", "", r["title"].casefold())
            if normalized:
                groups[normalized].append(r["record_id"])
        review["duplicate_title_groups"] = [
            {
                "record_ids": ids[:10],
                "count": len(ids),
                "meaning": "title similarity only; not proof of duplicate content",
            }
            for ids in groups.values()
            if len(ids) > 1
        ][:10]
        search = SearchService(c)
        for r in recent[:5]:
            if not r["title"].strip():
                continue
            hits = search.keyword(r["title"][:120], limit=5)
            review["keyword_smoke"].append(
                {
                    "record_id": r["record_id"],
                    "self_title_found_in_top5": any(
                        h.record_id == r["record_id"] for h in hits
                    ),
                }
            )
        review["state"] = "measured"
        review["bounds"] = {
            "candidate_limit": limit,
            "title_sample_max": 500,
            "keyword_smoke_max": 5,
        }
        review["interpretation"] = (
            "先用get按需读取候选记录，再判断过时、矛盾、重复或标签问题。"
            "标题自检是基础可检索性烟测，不是独立金标评测或语义质量分数。"
            "不返回私人金标、测试集或任意文件；本接口只读取。"
            "自主变更使用maintain的备份、版本和事务门禁，不需逐项人工确认。"
        )
    except (OSError, sqlite3.Error, ValueError):
        review["state"] = "unknown"
        result["complete"] = False
        result["findings"].append({"code": "review_incomplete", "severity": "unknown"})
    finally:
        if c is not None:
            c.close()
    return result
