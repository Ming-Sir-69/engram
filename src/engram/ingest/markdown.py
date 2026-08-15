"""把 Second Brain 的 Markdown 切成条目。

切分规则：**顶层 `- ` bullet 就是一条记录**，其缩进子项归入该条目正文。
这与源文件的书写习惯一致——每个顶层 bullet 本来就是一个独立想法。

`body` 保留原始块（仅剥掉顶层的 `- ` 前缀），因此加回前缀即可还原原文，
反向导出依赖这一性质。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6}) (.+)$")
_BOLD = re.compile(r"^\*\*(.+?)\*\*")
_DATE_PAREN = re.compile(r"^[（(](\d{4}-\d{2}-\d{2})[)）]")
_LEADING_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})[：:]\s*")
_TEMPLATE_MARKER = "记录模板："
_PLACEHOLDERS = frozenset({"（空）", "(空)", ""})
_SEPARATORS = "：。；:;"
_MAX_TITLE = 40
_MIN_PREFIX = 4
_TRUNCATE = 30


@dataclass(frozen=True, slots=True)
class SourceEntry:
    source_file: str
    start_line: int
    end_line: int
    heading_path: tuple[str, ...]
    title: str
    body: str
    recorded_at: str | None = None


def _derive_title(first_line: str) -> tuple[str, str | None]:
    """从条目首行派生标题与记录日期。

    正文永远保留完整内容，标题只影响展示——宁可标题朴素，不为好看截内容。
    """
    recorded_at: str | None = None
    line = first_line.strip()

    leading = _LEADING_DATE.match(line)
    if leading:
        recorded_at = leading.group(1)
        line = line[leading.end() :]

    bold = _BOLD.match(line)
    if bold:
        rest = line[bold.end() :]
        paren = _DATE_PAREN.match(rest)
        if paren and recorded_at is None:
            recorded_at = paren.group(1)
        return bold.group(1).strip(), recorded_at

    stripped = line.rstrip("。.")
    if len(stripped) <= _MAX_TITLE:
        return stripped, recorded_at

    index = min(
        (line.find(sep) for sep in _SEPARATORS if line.find(sep) > 0),
        default=-1,
    )
    if index > 0:
        prefix = line[:index].strip()
        if _MIN_PREFIX <= len(prefix) <= _MAX_TITLE:
            return prefix, recorded_at
    return line[:_TRUNCATE] + "…", recorded_at


def parse_markdown(text: str, *, source_file: str) -> list[SourceEntry]:
    entries: list[SourceEntry] = []
    headings: dict[int, str] = {}
    block: list[str] = []
    start = 0
    end = 0
    path: tuple[str, ...] = ()
    skipping = False

    def flush() -> None:
        nonlocal block
        if not block:
            return
        body = "\n".join(block).rstrip()
        block = []
        if body.strip() in _PLACEHOLDERS:
            return
        title, recorded_at = _derive_title(body.splitlines()[0])
        if not title:
            return
        entries.append(
            SourceEntry(
                source_file=source_file,
                start_line=start,
                end_line=end,
                heading_path=path,
                title=title,
                body=body,
                recorded_at=recorded_at,
            )
        )

    for number, line in enumerate(text.splitlines(), start=1):
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            headings[level] = heading.group(2).strip()
            for deeper in [key for key in headings if key > level]:
                del headings[deeper]
            path = tuple(headings[key] for key in sorted(headings))
            skipping = False
            continue

        if line.strip().endswith(_TEMPLATE_MARKER):
            # 模板块是书写脚手架而非内容，跳到下一个标题为止。
            flush()
            skipping = True
            continue
        if skipping:
            continue

        if line.startswith("- "):
            flush()
            block = [line[2:]]
            start = number
            end = number
        elif block and line[:1].isspace() and line.strip():
            block.append(line)
            end = number

    flush()
    return entries
