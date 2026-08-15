from engram.ingest.markdown import SourceEntry, parse_markdown


def _parse(text: str) -> list[SourceEntry]:
    return parse_markdown(text, source_file="t.md")


def test_heading_path_tracks_nesting() -> None:
    entries = _parse("# A\n\n## B\n\n### C\n\n- 条目一\n")
    assert entries[0].heading_path == ("A", "B", "C")


def test_deeper_headings_are_dropped_when_level_rises() -> None:
    entries = _parse("# A\n\n## B\n\n### C\n\n## D\n\n- 条目\n")
    assert entries[0].heading_path == ("A", "D")


def test_template_block_is_skipped_until_next_heading() -> None:
    text = "## 观点\n\n记录模板：\n\n- 主题：\n- 我的结论：\n\n### 待写\n\n- 真实条目\n"
    entries = _parse(text)
    assert [entry.title for entry in entries] == ["真实条目"]


def test_sub_bullets_fold_into_parent() -> None:
    text = "## S\n\n- **父条目**\n  - 子项甲\n  - 子项乙\n\n- 另一条\n"
    entries = _parse(text)
    assert len(entries) == 2
    assert entries[0].body == "**父条目**\n  - 子项甲\n  - 子项乙"
    assert entries[1].title == "另一条"


def test_empty_placeholder_is_excluded() -> None:
    assert _parse("## S\n\n- （空）\n") == []


def test_bold_title_and_trailing_date_are_split() -> None:
    text = "## S\n\n- **阶段门校验插件**（2026-01-02）：面向多 Agent 的治理工具。\n"
    entry = _parse(text)[0]
    assert entry.title == "阶段门校验插件"
    assert entry.recorded_at == "2026-01-02"
    assert "面向多 Agent 的治理工具" in entry.body


def test_leading_date_prefix_does_not_become_the_title() -> None:
    """`- 2026-01-03：**待讨论**——...` 的标题必须是内容而不是日期。"""
    text = "## S\n\n- 2026-01-03：**待确认/待复核**——两种形态的分工策略。\n"
    entry = _parse(text)[0]
    assert entry.recorded_at == "2026-01-03"
    assert entry.title == "待确认/待复核"


def test_short_plain_line_becomes_whole_title() -> None:
    entry = _parse("## S\n\n- 折叠支架限位结构。\n")[0]
    assert entry.title == "折叠支架限位结构"


def test_long_plain_line_splits_title_at_first_separator() -> None:
    text = (
        "## S\n\n- 地铁隧道内的低频噪声：可作为过渡音效，"
        "整段听下来像是把通勤路上的环境声重新编排成了节奏。\n"
    )
    entry = _parse(text)[0]
    assert entry.title == "地铁隧道内的低频噪声"
    assert "重新编排成了节奏" in entry.body


def test_title_falls_back_to_truncation_when_prefix_too_short() -> None:
    text = "## S\n\n- 阀盖：" + "四个圆弧遥控解锁弹簧锁定杯子加入卡扣" * 4 + "。\n"
    entry = _parse(text)[0]
    assert entry.title.endswith("…")
    assert len(entry.title) <= 31


def test_line_range_spans_sub_bullets() -> None:
    text = "# A\n\n- 父\n  - 子一\n  - 子二\n"
    entry = _parse(text)[0]
    assert (entry.start_line, entry.end_line) == (3, 5)


def test_blockquote_and_table_rows_are_ignored() -> None:
    text = "## S\n\n> 引言\n\n| a | b |\n|---|---|\n\n- 条目\n"
    entries = _parse(text)
    assert [entry.title for entry in entries] == ["条目"]


def test_body_round_trips_to_original_block() -> None:
    """body 加回 `- ` 前缀必须还原原始块，这是反向导出的基础。"""
    block = "- **父**（2026-01-02）\n  - 子一\n  - 子二"
    entry = _parse(f"# A\n\n{block}\n")[0]
    assert f"- {entry.body}" == block


def test_source_file_is_recorded() -> None:
    assert _parse("# A\n\n- 条目\n")[0].source_file == "t.md"
