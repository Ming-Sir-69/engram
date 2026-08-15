"""种子标签映射表。

映射表是用户配置而不是内置常量：标题结构因人而异，写死在代码里对别人无效，
也会把个人笔记的目录结构一并发布出去。因此这里测的是"读取 + 校验 + 合并"
三件事，用合成标题，不依赖任何真实笔记。

真实源文件的覆盖率不在单测里验：那需要读本机私有文件，且换一台机器就失效。
覆盖率由 `migrate from-markdown --dry-run` 报告的 `unseeded` 计数承担，在真实
数据上、迁移当场判定。
"""

import json
from pathlib import Path

import pytest

from engram.errors import InvalidInputError
from engram.ingest.taxonomy import (
    MAX_SEED_DOMAINS,
    MAX_SEED_TAGS,
    load_seed_taxonomy,
    seed_labels,
)

_TAXONOMY = {
    "顶层": (("tooling",), ("stance",)),
    "机械": (("product-design",), ("mechanism-design",)),
    "声音": (("product-design",), ("audio",)),
    "堆叠": (("tooling", "life", "product-design"), ("a", "b", "c", "d", "e")),
    "重复": (("tooling",), ("stance",)),
}


def _write(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "seed_taxonomy.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_specific_heading_yields_domain_and_tag() -> None:
    domains, tags = seed_labels(("顶层", "1. 机械"), _TAXONOMY)
    assert "product-design" in domains
    assert "mechanism-design" in tags


def test_parent_and_child_headings_merge() -> None:
    """父标题的性质标签与子标题的领域标签都要保留。"""
    domains, tags = seed_labels(("顶层", "2. 声音"), _TAXONOMY)
    assert "tooling" in domains
    assert "stance" in tags
    assert "audio" in tags


def test_numbered_prefix_is_normalised() -> None:
    assert seed_labels(("1. 机械",), _TAXONOMY) == seed_labels(("机械",), _TAXONOMY)


def test_unknown_heading_yields_nothing() -> None:
    assert seed_labels(("完全没见过的标题",), _TAXONOMY) == ((), ())


def test_empty_path_yields_nothing() -> None:
    assert seed_labels((), _TAXONOMY) == ((), ())


def test_absent_taxonomy_yields_nothing() -> None:
    """没有配置映射表时迁移照常进行，只是不带种子标签。"""
    assert seed_labels(("顶层", "1. 机械")) == ((), ())


def test_results_are_capped() -> None:
    domains, tags = seed_labels(("堆叠",), _TAXONOMY)
    assert len(domains) == MAX_SEED_DOMAINS
    assert len(tags) == MAX_SEED_TAGS


def test_results_are_deduplicated_and_ordered() -> None:
    domains, tags = seed_labels(("顶层", "重复"), _TAXONOMY)
    assert domains == ("tooling",)
    assert tags == ("stance",)


def test_missing_config_file_loads_empty(tmp_path: Path) -> None:
    assert load_seed_taxonomy(tmp_path / "absent.json") == {}


def test_loader_normalises_and_shapes_entries(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"1. 机械": {"domains": ["product-design"], "tags": ["mechanism-design"]}},
    )
    loaded = load_seed_taxonomy(path)
    assert loaded == {"机械": (("product-design",), ("mechanism-design",))}
    assert seed_labels(("机械",), loaded) == (
        ("product-design",),
        ("mechanism-design",),
    )


def test_unknown_domain_is_rejected(tmp_path: Path) -> None:
    """映射表写错 domain 名会静默污染整库分类，必须在加载时挡住。"""
    path = _write(tmp_path, {"机械": {"domains": ["not-a-domain"], "tags": []}})
    with pytest.raises(InvalidInputError) as error:
        load_seed_taxonomy(path)
    assert error.value.context["problems"] == ["机械 -> domain not-a-domain"]


def test_malformed_tag_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, {"机械": {"domains": [], "tags": ["Not A Tag"]}})
    with pytest.raises(InvalidInputError) as error:
        load_seed_taxonomy(path)
    assert error.value.context["problems"] == ["机械 -> tag Not A Tag"]


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "seed_taxonomy.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(InvalidInputError) as error:
        load_seed_taxonomy(path)
    assert "reason" in error.value.context
