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
import shutil
import time
from pathlib import Path

from engram.errors import InvalidInputError
from engram.repository import RecordRepository

DERIVED_MARKER = "<!-- engram: derived view, do not edit -->"


def _is_derived(path: Path) -> bool:
    with path.open(encoding="utf-8") as handle:
        return handle.readline().rstrip("\n") == DERIVED_MARKER


def _assert_writable(path: Path) -> None:
    if not path.exists():
        return
    if not _is_derived(path):
        raise InvalidInputError(
            "refusing to overwrite a file that engram did not generate",
            context={"path": str(path), "expected_first_line": DERIVED_MARKER},
        )


def _adopt(out_dir: Path, names: list[str]) -> int:
    """把人工维护的文件正式交由 engram 生成，先留一份原件。

    接管是一次不可逆的覆盖。没有备份的接管，等于把多年笔记压在一条命令上。
    """
    stale = [
        name
        for name in names
        if (out_dir / name).exists() and not _is_derived(out_dir / name)
    ]
    if not stale:
        return 0
    backup = out_dir / f".engram-backup-{time.strftime('%Y%m%d%H%M%S')}"
    backup.mkdir(parents=True, exist_ok=True)
    for name in stale:
        shutil.copy2(out_dir / name, backup / name)
    return len(stale)


def _rows(repository: RecordRepository):
    return repository.connection.execute(
        "SELECT record_id, title, body, record_type, attributes_json FROM records"
    ).fetchall()


def export_markdown(
    *, repository: RecordRepository, out_dir: Path, adopt: bool = False
) -> dict[str, int]:
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
    if adopt:
        backed_up = _adopt(out_dir, sorted(grouped))
    else:
        backed_up = 0
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
    return {"files": len(grouped), "records": records, "backed_up": backed_up}


def export_index(*, repository: RecordRepository, out_file: Path) -> dict[str, int]:
    """生成一份"库里有什么"的地图，不含正文。

    这份索引是唯一适合常驻调用方上下文的东西：它用几百字符说明库的规模与
    构成，让调用方能判断该不该检索、该往哪个方向检索。把全文搬进上下文
    则会把向量库的价值整个抵消掉——检索存在的意义正是不必这么做。
    """
    out_file = Path(out_file)
    connection = repository.connection
    total = repository.count()
    lines = [
        DERIVED_MARKER,
        "",
        "# 知识库索引",
        "",
        (
            f"共 {total} 条记录。**正文不在本文件里**——用 engram MCP 的 `recall` "
            "检索、`get` 取全文。下面只说明库里有什么，供判断该不该查、往哪个方向查。"
        ),
        "",
    ]
    for kind, heading in (("domain", "领域"), ("tag", "标签")):
        rows = connection.execute(
            "SELECT value, COUNT(*) AS n FROM facets WHERE kind = ? "
            "GROUP BY value ORDER BY n DESC, value",
            (kind,),
        ).fetchall()
        if not rows:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        lines.append("、".join(f"{row['value']} {row['n']}" for row in rows))
        lines.append("")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines), encoding="utf-8")
    return {"records": total, "bytes": out_file.stat().st_size}


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
