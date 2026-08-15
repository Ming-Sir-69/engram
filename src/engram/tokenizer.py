from __future__ import annotations

import re

_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_.:/-]*")
_PLAIN_PART = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_CJK_RUN = re.compile(r"[㐀-鿿]+")


def tokenize(text: str) -> list[str]:
    """切出可检索 token。

    标识符同时保留完整形式与斜杠拆分后的子串，避免
    `browser-use/video-use` 这类复合标识符只能整体命中。
    中文按单字与相邻二元组切分，兼顾精确与模糊匹配。
    """
    lowered = text.lower()
    identifiers: list[str] = []
    for raw in _IDENTIFIER.findall(lowered):
        # 剥掉首尾分隔符：中文路径会让匹配停在 "item_fille/" 这种
        # 带尾斜杠的形式上，若不剥离则拆分出空串而整体失效。
        identifier = raw.strip("/.:-")
        if not identifier:
            continue
        identifiers.append(identifier)
        parts = [part for part in identifier.split("/") if part]
        if len(parts) > 1 and all(_PLAIN_PART.fullmatch(part) for part in parts):
            identifiers.extend(parts)
    chinese: list[str] = []
    for sequence in _CJK_RUN.findall(lowered):
        chinese.extend(sequence)
        chinese.extend(
            sequence[index : index + 2] for index in range(len(sequence) - 1)
        )
    return identifiers + chinese


def fts_document(text: str) -> str:
    return " ".join(tokenize(text))
