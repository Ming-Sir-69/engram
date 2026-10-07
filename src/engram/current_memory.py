"""Explicit correction metadata and a derived, read-only current view.

The stored record remains the source of truth.  This module never infers a
contradiction from age or similarity and never rewrites the original body.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from engram.errors import InvalidInputError, RecordNotFoundError

CORRECTION_RELATIONS = ("supersedes", "corrects")


def normalize_corrections(value: object) -> list[dict[str, object]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise InvalidInputError("corrections must be a list of objects")
    result: list[dict[str, object]] = []
    allowed = {
        "target_id",
        "scope",
        "source",
        "range",
        "target_excerpt",
        "baseline_hash",
        "target_revision",
        "target_hash",
    }
    for item in value:
        if not isinstance(item, Mapping) or set(item) - allowed:
            raise InvalidInputError("invalid correction fields")
        normalized: dict[str, object] = {}
        for name in ("target_id", "scope", "source"):
            text = item.get(name)
            if not isinstance(text, str) or not text.strip():
                raise InvalidInputError(f"correction {name} must be non-empty text")
            normalized[name] = text.strip()
        if normalized["scope"] not in ("whole-record", "partial"):
            raise InvalidInputError("correction scope must be whole-record or partial")
        for name in ("range", "target_excerpt", "baseline_hash"):
            if name not in item:
                continue
            text = item[name]
            if not isinstance(text, str) or not text.strip():
                raise InvalidInputError(f"correction {name} must be non-empty text")
            # Exact excerpts are intentionally not stripped: whitespace is content.
            normalized[name] = text if name == "target_excerpt" else text.strip()
        if normalized["scope"] == "partial" and not (
            normalized.get("range") or normalized.get("target_excerpt")
        ):
            raise InvalidInputError("partial correction needs range or target_excerpt")
        if normalized not in result:
            result.append(normalized)
    return result


def validate_corrections(
    connection: sqlite3.Connection,
    record_id: str,
    corrections: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Called under the same write transaction as the body/metadata commit."""
    validated: list[dict[str, object]] = []
    for correction in corrections:
        target_id = str(correction["target_id"])
        if target_id == record_id:
            raise InvalidInputError("a record cannot correct itself")
        target = connection.execute(
            "SELECT body, content_hash, revision FROM records WHERE record_id = ?",
            (target_id,),
        ).fetchone()
        if target is None:
            raise RecordNotFoundError(target_id)
        baseline = correction.get("baseline_hash")
        if baseline is not None and baseline != target["content_hash"]:
            raise InvalidInputError(
                "correction baseline changed", context={"target_id": target_id}
            )
        excerpt = correction.get("target_excerpt")
        if excerpt is not None and target["body"].count(str(excerpt)) != 1:
            raise InvalidInputError(
                "target_excerpt must match exactly once in the target body",
                context={"target_id": target_id},
            )
        # Follow source -> target ancestry. Adding this edge must not create a cycle.
        row = connection.execute(
            """
            WITH RECURSIVE ancestors(record_id) AS (
                SELECT ?
                UNION
                SELECT l.target_id FROM record_links l
                JOIN ancestors a ON l.source_id = a.record_id
                WHERE l.relation IN ('supersedes', 'corrects')
            ) SELECT 1 FROM ancestors WHERE record_id = ? LIMIT 1
            """,
            (target_id, record_id),
        ).fetchone()
        if row is not None:
            raise InvalidInputError("correction would create a cycle")
        validated.append(
            {
                **correction,
                "target_revision": target["revision"],
                "target_hash": target["content_hash"],
            }
        )
    return validated


def write_correction_links(
    connection: sqlite3.Connection, record_id: str, corrections: list[dict[str, object]]
) -> None:
    for correction in corrections:
        relation = "supersedes" if correction["scope"] == "whole-record" else "corrects"
        connection.execute(
            "INSERT INTO record_links(source_id, target_id, relation, score, provenance) "
            "VALUES (?, ?, ?, 1.0, 'explicit') ON CONFLICT DO NOTHING",
            (record_id, correction["target_id"], relation),
        )


@dataclass(frozen=True, slots=True)
class CurrentProjection:
    record_id: str
    title: str
    body: str
    current_text: str
    payload: dict[str, object]


class CurrentMemoryIndex:
    """One query's correction graph; metadata is small, bodies are loaded on demand."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.records: dict[str, dict[str, object]] = {}
        self.children: dict[str, list[dict[str, object]]] = defaultdict(list)
        self.parents: dict[str, list[str]] = defaultdict(list)
        rows = connection.execute(
            "SELECT DISTINCT r.record_id, r.attributes_json FROM records r "
            "JOIN record_links l ON l.source_id = r.record_id "
            "WHERE l.relation IN ('supersedes', 'corrects') ORDER BY r.record_id"
        ).fetchall()
        for row in rows:
            for metadata in json.loads(row["attributes_json"]).get("corrections", []):
                if not isinstance(metadata, dict) or metadata.get("scope") not in (
                    "whole-record",
                    "partial",
                ):
                    continue
                source_id, target_id = row["record_id"], metadata.get("target_id")
                if not isinstance(target_id, str):
                    continue
                edge = {**metadata, "record_id": source_id}
                self.children[target_id].append(edge)
                if target_id not in self.parents[source_id]:
                    self.parents[source_id].append(target_id)

    @property
    def has_corrections(self) -> bool:
        return bool(self.children)

    def record(self, record_id: str) -> dict[str, object]:
        if record_id not in self.records:
            row = self.connection.execute(
                "SELECT * FROM records WHERE record_id = ?", (record_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(record_id)
            value = dict(row)
            value["projects"] = [
                item[0]
                for item in self.connection.execute(
                    "SELECT project FROM record_projects WHERE record_id = ? ORDER BY project",
                    (record_id,),
                )
            ]
            self.records[record_id] = value
        return self.records[record_id]

    def roots(self, record_id: str) -> list[str]:
        roots: list[str] = []
        visited: set[str] = set()

        def visit(current: str) -> None:
            if current in visited:
                return
            visited.add(current)
            parents = self.parents.get(current, [])
            if not parents:
                roots.append(current)
            for parent in parents:
                visit(parent)

        visit(record_id)
        return roots

    def successors(
        self, record_id: str, seen: frozenset[str] = frozenset()
    ) -> list[str]:
        if record_id in seen:
            return []
        whole = [
            edge
            for edge in self.children.get(record_id, [])
            if edge["scope"] == "whole-record"
        ]
        if whole:
            return list(
                dict.fromkeys(
                    successor
                    for edge in whole
                    for successor in self.successors(
                        str(edge["record_id"]), seen | {record_id}
                    )
                )
            )
        return [record_id] if self.record(record_id)["status"] == "active" else []

    def chain(self, roots: list[str]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        seen: set[str] = set()

        def visit(record_id: str) -> None:
            if record_id in seen:
                return
            seen.add(record_id)
            record = self.record(record_id)
            result.append(
                {
                    name: record[name]
                    for name in (
                        "record_id",
                        "title",
                        "body",
                        "status",
                        "revision",
                        "content_hash",
                        "source_agent",
                        "updated_at",
                        "projects",
                    )
                }
                | {
                    "corrections": [
                        dict(edge)
                        for parent in self.parents.get(record_id, [])
                        for edge in self.children[parent]
                        if edge["record_id"] == record_id
                    ]
                }
            )
            for edge in self.children.get(record_id, []):
                visit(str(edge["record_id"]))

        for root in roots:
            visit(root)
        return result

    def _text(
        self, record_id: str, seen: frozenset[str] = frozenset()
    ) -> tuple[str, list[dict[str, object]]]:
        record = self.record(record_id)
        original = str(record["body"])
        notes: list[dict[str, object]] = []
        if record_id in seen:
            return original, notes
        planned: list[dict[str, object]] = []
        for edge in self.children.get(record_id, []):
            if edge["scope"] != "partial":
                continue
            source_id = str(edge["record_id"])
            effective = self.successors(source_id)
            replacement_views = [
                self._text(item, seen | {record_id}) for item in effective
            ]
            replacements = [item[0] for item in replacement_views]
            replacement = (
                "\n\n".join(replacements)
                if replacements
                else "[此范围已明确纠正；纠正来源已归档，当前值待核实]"
            )
            excerpt = edge.get("target_excerpt")
            baseline_changed = edge.get("target_hash") != record["content_hash"]
            exact = (
                not baseline_changed
                and isinstance(excerpt, str)
                and original.count(excerpt) == 1
            )
            start = original.index(excerpt) if exact else -1
            planned.append(
                {
                    "edge": edge,
                    "replacement": replacement,
                    "nested_notes": [
                        note for _text, nested in replacement_views for note in nested
                    ],
                    "note": {
                        **edge,
                        "effective_record_ids": effective,
                        "applied_exactly": exact,
                        "baseline_changed": baseline_changed,
                        "overlapping_scope": False,
                    },
                    "start": start,
                    "end": start + len(str(excerpt)) if exact else -1,
                }
            )

        # Locate every edit on the immutable source baseline. Never search text
        # inserted by an earlier correction: that would modify a different claim.
        for offset, left in enumerate(planned):
            if left["start"] < 0:
                continue
            for right in planned[offset + 1 :]:
                if right["start"] >= 0 and max(left["start"], right["start"]) < min(
                    left["end"], right["end"]
                ):
                    left["note"]["overlapping_scope"] = True
                    right["note"]["overlapping_scope"] = True
                    left["note"]["applied_exactly"] = False
                    right["note"]["applied_exactly"] = False

        pieces: list[str] = []
        cursor = 0
        for edit in sorted(planned, key=lambda item: item["start"]):
            if not edit["note"]["applied_exactly"]:
                continue
            pieces.extend((original[cursor : edit["start"]], str(edit["replacement"])))
            cursor = edit["end"]
        pieces.append(original[cursor:])
        text = "".join(pieces)
        unresolved = any(
            not item["note"]["applied_exactly"] and not item["note"]["baseline_changed"]
            for item in planned
        )
        stale = any(item["note"]["baseline_changed"] for item in planned)
        if unresolved:
            text = (
                "[原记录资料：仅纠正范围外的内容仍适用；未定位或重叠的纠正范围内原文属于历史资料]\n"
                + text
            )
        elif stale:
            text = (
                "[当前正文：下列历史局部纠正的基线已变化，未自动应用；请核实其范围是否仍适用]\n"
                + text
            )
        for edit in planned:
            note = edit["note"]
            if not note["applied_exactly"]:
                edge = edit["edge"]
                scope = str(
                    edge.get("range") or edge.get("target_excerpt") or "指定局部"
                )
                if note["baseline_changed"]:
                    label = "历史局部纠正（基线已变化，未自动应用当前正文）"
                elif note["overlapping_scope"]:
                    label = "重叠局部纠正（并存资料，适用性待核实）"
                else:
                    label = "明确局部纠正（该范围的原文仅为历史资料）"
                text += f"\n\n[{label}：{scope}]\n{edit['replacement']}"
            notes.append(note)
            notes.extend(edit["nested_notes"])
        return text, notes

    def project(self, record_id: str) -> list[CurrentProjection]:
        roots = self.roots(record_id)
        anchors = list(
            dict.fromkeys(item for root in roots for item in self.successors(root))
        )
        chain = self.chain(roots)
        corrected = len(chain) > 1
        result: list[CurrentProjection] = []
        for anchor in anchors:
            record = self.record(anchor)
            text, partial = self._text(anchor)
            payload: dict[str, object] = {
                "view": "current",
                "status": "partially_corrected" if partial else "current",
                "record_id": anchor,
                "revision": record["revision"],
                "content_hash": record["content_hash"],
                "body": record["body"],
                "body_role": "source_original",
                "current_text": text,
                "projects": list(
                    dict.fromkeys(
                        project for item in chain for project in item["projects"]
                    )
                ),
                "correction_chain": chain if corrected else [],
                "partial_corrections": partial,
                "coexisting_record_ids": anchors if len(anchors) > 1 else [],
            }
            result.append(
                CurrentProjection(
                    anchor, str(record["title"]), str(record["body"]), text, payload
                )
            )
        return result

    def describe(
        self, record_id: str, *, view: str = "current"
    ) -> dict[str, object] | None:
        record = self.record(record_id)
        roots = self.roots(record_id)
        chain = self.chain(roots)
        if view == "current" and len(chain) == 1:
            return None
        projected = self.project(record_id)
        whole = any(
            edge["scope"] == "whole-record" for edge in self.children.get(record_id, [])
        )
        applicable: set[str] = set()

        def include(anchor: str) -> None:
            if anchor in applicable:
                return
            applicable.add(anchor)
            for edge in self.children.get(anchor, []):
                if edge["scope"] == "partial":
                    for successor in self.successors(str(edge["record_id"])):
                        include(successor)

        for item in projected:
            include(item.record_id)
        status = "current"
        if whole:
            status = "superseded"
        elif record["status"] != "active":
            status = "archived"
        elif record_id not in applicable:
            status = "superseded_scope"
        elif self.parents.get(record_id) and record_id not in {
            item.record_id for item in projected
        }:
            status = "correction_source"
        elif any(
            item.payload["partial_corrections"]
            for item in projected
            if item.record_id == record_id
        ):
            status = "partially_corrected"
        return {
            "view": view,
            "source_record_id": record_id,
            "source_revision": record["revision"],
            "status": status,
            "current_records": [item.payload for item in projected],
            "correction_chain": chain,
            "current_available": bool(projected),
        }
