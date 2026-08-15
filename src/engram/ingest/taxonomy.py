"""源文件标题 → 种子标签。

Markdown 笔记的 `##`/`###` 标题是长期人工维护的分类结果。迁移之后标题结构
就不存在了，**这是唯一一次把它转成结构化标签的机会**。不收割等于扔掉已有
标注、再花算力让模型重新猜，而且猜得更差。

收割出来的标签置信度高于模型输出，直接以 `rule` 来源写入，同时为后续新记录
的 kNN 继承提供高质量近邻——冷启动问题因此在迁移当天就被解决。

映射表本身**不内置**：标题是各人自己的分类习惯，写死在代码里既对别人无效，
也等于把私人笔记的目录结构发布出去。它从数据目录下的配置文件读取，缺省为空
（迁移照常进行，只是 `unseeded` 计数会等于总条数）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from engram.errors import InvalidInputError

MAX_SEED_DOMAINS = 2
MAX_SEED_TAGS = 4
SEED_CONFIDENCE = 0.9

SeedTaxonomy = dict[str, tuple[tuple[str, ...], tuple[str, ...]]]

_NUMBER_PREFIX = re.compile(r"^\d+[.、]\s*")
_LABEL = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _normalise(heading: str) -> str:
    return _NUMBER_PREFIX.sub("", heading).strip()


def load_seed_taxonomy(path: Path) -> SeedTaxonomy:
    """读取标题映射表。

    格式：`{"标题": {"domains": [...], "tags": [...]}}`。domain 必须在受控词表
    内、tag 必须是合法 label——映射表写错一个名字就会静默污染整库分类，所以
    这里宁可拒绝加载也不做容错。
    """
    path = Path(path)
    if not path.is_file():
        return {}
    # 延迟导入：词表在 classify 模块，而 classify 会带进向量栈。
    # 迁移属于纯写入路径，不应该因为一张映射表就依赖嵌入实现。
    from engram.classify import DOMAIN_VOCABULARY

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise InvalidInputError(
            "seed taxonomy is not valid JSON",
            context={"path": str(path), "reason": str(error)},
        ) from error

    taxonomy: SeedTaxonomy = {}
    problems: list[str] = []
    for heading, entry in raw.items():
        domains = tuple(entry.get("domains", ()))
        tags = tuple(entry.get("tags", ()))
        problems.extend(
            f"{heading} -> domain {domain}"
            for domain in domains
            if domain not in DOMAIN_VOCABULARY
        )
        problems.extend(
            f"{heading} -> tag {tag}" for tag in tags if not _LABEL.fullmatch(tag)
        )
        taxonomy[_normalise(heading)] = (domains, tags)
    if problems:
        raise InvalidInputError(
            "seed taxonomy contains unknown domains or malformed tags",
            context={"path": str(path), "problems": problems},
        )
    return taxonomy


def seed_labels(
    heading_path: tuple[str, ...],
    taxonomy: SeedTaxonomy | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """沿标题路径逐层查表并合并，保持出现顺序、去重、截断。

    路径上每一层都会查表：父标题通常贡献性质（观点/灵感/假设），
    子标题贡献领域。
    """
    if not taxonomy:
        return ((), ())
    domains: list[str] = []
    tags: list[str] = []
    for heading in heading_path:
        entry = taxonomy.get(_normalise(heading))
        if entry is None:
            continue
        for domain in entry[0]:
            if domain not in domains:
                domains.append(domain)
        for tag in entry[1]:
            if tag not in tags:
                tags.append(tag)
    return tuple(domains[:MAX_SEED_DOMAINS]), tuple(tags[:MAX_SEED_TAGS])
