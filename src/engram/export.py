"""把 SQLite 里的记录导回 Markdown 与 JSONL。

迁移把事实源从 Markdown 换成了 SQLite，导出就是这次翻转的对价：**任何时候
都能回到纯文本**。没有它，内容就被锁死在一个 schema 里，工具一旦停止维护
就等于数据丢失。

写入前必须确认目标文件是自己生成的（首行带派生标记）。源文件严格只读，
而"把源目录填成导出目录"是最容易犯、后果最严重的一次误操作——所以这里
不靠约定，靠拒绝写入。
"""

from __future__ import annotations

import json
from pathlib import Path

from engram.errors import InvalidInputError
from engram.repository import RecordRepository

DERIVED_MARKER = "<!-- engram: derived view, do not edit -->"


def _assert_writable(path: Path) -> None:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        first = handle.readline().rstrip("\n")
    if first != DERIVED_MARKER:
        raise InvalidInputError(
            "refusing to overwrite a file that engram did not generate",
            context={"path": str(path), "expected_first_line": DERIVED_MARKER},
        )


def _rows(repository: RecordRepository):
    return repository.connection.execute(
        "SELECT record_id, title, body, record_type, attributes_json FROM records"
    ).fetchall()


def export_markdown(*, repository: RecordRepository, out_dir: Path) -> dict[str, int]:
    out_dir = Path(out_dir)
    grouped: dict[str, list[tuple[int, list[str], str]]] = {}
    for row in _rows(repository):
        attributes = json.loads(row["attributes_json"])
        source_file = str(attributes.get("source_file", "unsorted.md"))
        grouped.setdefault(source_file, []).append(
            (
                int(attributes.get("start_line", 0)),
                list(attributes.get("heading_path", [])),
                row["body"],
            )
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    for name in sorted(grouped):
        _assert_writable(out_dir / name)

    records = 0
    for name, entries in sorted(grouped.items()):
        lines = [DERIVED_MARKER, ""]
        current: list[str] = []
        for _, heading_path, body in sorted(entries, key=lambda item: item[0]):
            for level, heading in enumerate(heading_path, start=1):
                if len(current) >= level and current[level - 1] == heading:
                    continue
                lines.append(f"{'#' * level} {heading}")
                lines.append("")
                current = [*heading_path[: level - 1], heading]
            current = list(heading_path)
            lines.append(f"- {body}")
            records += 1
        lines.append("")
        (out_dir / name).write_text("\n".join(lines), encoding="utf-8")
    return {"files": len(grouped), "records": records}


def export_jsonl(*, repository: RecordRepository, out_file: Path) -> dict[str, int]:
    out_file = Path(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    facets: dict[str, list[dict[str, object]]] = {}
    for row in repository.connection.execute(
        "SELECT record_id, kind, value, provenance, confidence, locked FROM facets "
        "ORDER BY record_id, kind, value"
    ):
        facets.setdefault(row["record_id"], []).append(
            {
                "kind": row["kind"],
                "value": row["value"],
                "provenance": row["provenance"],
                "confidence": row["confidence"],
                "locked": bool(row["locked"]),
            }
        )

    records = 0
    with out_file.open("w", encoding="utf-8") as handle:
        for row in _rows(repository):
            payload = {
                "record_id": row["record_id"],
                "record_type": row["record_type"],
                "title": row["title"],
                "body": row["body"],
                "attributes": json.loads(row["attributes_json"]),
                "facets": facets.get(row["record_id"], []),
            }
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            records += 1
    return {"records": records}
