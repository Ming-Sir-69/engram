from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from engram.domain import Facet, Record
from engram.errors import ModelUnavailableError
from engram.vectors import VectorStore

_URL = re.compile(r"https?://\S+")
_MAX_LABEL_LENGTH = 40
_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEFAULT_DOMAIN = "unsorted"

# 领域是封闭集合：模型只做选择题而非自由生成，小模型才能稳定输出。
# 新增领域应通过维护接口显式扩充，不由模型自行发明。
DOMAIN_VOCABULARY = (
    "ai-engineering",
    "product-design",
    "ie-engineering",
    "career",
    "health",
    "learning",
    "creative",
    "life",
    "tooling",
)

_PROMPT = """你是知识库的分类器。为下面这条记录选择领域并给出标签。

领域只能从这个列表里选，最多选 2 个，选不出就返回空数组：
{vocabulary}

标签自由给出，最多 3 个，必须是小写英文，只能包含字母、数字和连字符。

只输出 JSON，格式为：{{"domains": [], "tags": []}}

标题：{title}
正文：{body}
"""


class LabelModel(Protocol):
    def label(self, title: str, body: str) -> dict[str, list[str]]: ...


@lru_cache(maxsize=1)
def _load_mlx_classifier(path: str):
    from mlx_lm import load

    return load(path)


class MLXLabelModel:
    """本地 MLX 分类器，仅当规则与近邻都无法分类时加载。"""

    def __init__(
        self,
        *,
        model_path: Path,
        runner: Callable[[str], str] | None = None,
    ) -> None:
        self.model_path = Path(model_path)
        self._runner = runner

    def _generate(self, prompt: str) -> str:
        if not (self.model_path / "config.json").is_file():
            raise ModelUnavailableError(
                f"local MLX classifier model is missing: {self.model_path}"
            )
        try:
            from mlx_lm import generate
            from mlx_lm.sample_utils import make_sampler

            model, tokenizer = _load_mlx_classifier(str(self.model_path))
            formatted = tokenizer.apply_chat_template(
                [{"role": "user", "content": prompt}],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            return generate(
                model,
                tokenizer,
                prompt=formatted,
                max_tokens=128,
                sampler=make_sampler(temp=0),
                verbose=False,
            )
        except (ImportError, OSError) as exc:
            raise ModelUnavailableError(
                f"local MLX classifier unavailable: {type(exc).__name__}"
            ) from exc

    def label(self, title: str, body: str) -> dict[str, list[str]]:
        prompt = _PROMPT.format(
            vocabulary="\n".join(f"- {name}" for name in DOMAIN_VOCABULARY),
            title=title[:200],
            body=body[:1500],
        )
        response = (self._runner or self._generate)(prompt)
        try:
            parsed = json.loads(response)
        except (TypeError, ValueError):
            return {"domains": [], "tags": []}
        if not isinstance(parsed, dict):
            return {"domains": [], "tags": []}
        domains = [
            value
            for value in _valid_labels(parsed.get("domains"))
            if value in DOMAIN_VOCABULARY
        ]
        return {"domains": domains[:2], "tags": _valid_labels(parsed.get("tags"))[:3]}


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    facets: tuple[Facet, ...]
    provenance: str
    needs_review: bool


def _valid_labels(values: object) -> list[str]:
    """过滤模型输出。

    超长或不符合 `^[a-z0-9][a-z0-9-]*$` 的标签一律丢弃：同样的输入只会
    得到同样的坏输出，重试没有意义，应直接降级。
    """
    if not isinstance(values, list):
        return []
    clean: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip().lower()
        if len(candidate) > _MAX_LABEL_LENGTH:
            continue
        if not _LABEL_PATTERN.fullmatch(candidate):
            continue
        clean.append(candidate)
    return clean


class Classifier:
    def __init__(
        self,
        *,
        store: VectorStore,
        model: LabelModel | None,
        knn_threshold: float = 0.55,
        knn_k: int = 5,
    ) -> None:
        self.store = store
        self.model = model
        self.knn_threshold = knn_threshold
        self.knn_k = knn_k

    def classify(
        self, record: Record, vector: list[float] | None
    ) -> ClassificationResult:
        trusted = self._trusted_layer(record)
        if trusted is not None:
            return trusted
        rule = self._rule_layer(record)
        if rule is not None:
            return rule
        if vector is not None:
            knn = self._knn_layer(record, vector)
            if knn is not None:
                return knn
        model_result = self._model_layer(record)
        if model_result is not None:
            return model_result
        return ClassificationResult(
            facets=(
                Facet(
                    record_id=record.record_id,
                    kind="domain",
                    value=DEFAULT_DOMAIN,
                    provenance="default",
                    confidence=0.0,
                ),
            ),
            provenance="default",
            needs_review=True,
        )

    def _trusted_layer(self, record: Record) -> ClassificationResult | None:
        """已有规则收割或人工确认的标签时，整条链直接短路。

        迁移会把源文件标题收割成 `rule` 标签，置信度高于 kNN 和模型；再猜一遍
        既浪费算力，也会用低置信度结果覆盖高置信度标注。`model` 来源不豁免。
        """
        rows = self.store.connection.execute(
            """
            SELECT kind, value, provenance, confidence, locked
            FROM facets
            WHERE record_id = ? AND (provenance IN ('rule','human') OR locked = 1)
            ORDER BY kind, value
            """,
            (record.record_id,),
        ).fetchall()
        if not rows:
            return None
        return ClassificationResult(
            facets=tuple(
                Facet(
                    record_id=record.record_id,
                    kind=row["kind"],
                    value=row["value"],
                    provenance=row["provenance"],
                    confidence=row["confidence"],
                    locked=bool(row["locked"]),
                )
                for row in rows
            ),
            provenance="rule",
            needs_review=False,
        )

    def _rule_layer(self, record: Record) -> ClassificationResult | None:
        if record.record_type == "reference" or _URL.search(record.body):
            return self._single_tag(record, "external-source")
        if record.record_type == "project":
            return self._single_tag(record, "project-status")
        return None

    @staticmethod
    def _single_tag(record: Record, value: str) -> ClassificationResult:
        return ClassificationResult(
            facets=(
                Facet(
                    record_id=record.record_id,
                    kind="tag",
                    value=value,
                    provenance="rule",
                    confidence=1.0,
                ),
            ),
            provenance="rule",
            needs_review=False,
        )

    def _knn_layer(
        self, record: Record, vector: list[float]
    ) -> ClassificationResult | None:
        neighbors = self.store.neighbors(
            vector, limit=self.knn_k, exclude=record.record_id
        )
        close = [
            (neighbor_id, score)
            for neighbor_id, score in neighbors
            if score >= self.knn_threshold
        ]
        if not close:
            return None
        # placeholders 只由 "?" 拼接而成，record_id 始终通过参数绑定传入，
        # 不存在注入面。
        placeholders = ",".join("?" for _ in close)
        rows = self.store.connection.execute(
            f"SELECT kind, value FROM facets WHERE record_id IN ({placeholders})",
            tuple(item[0] for item in close),
        ).fetchall()
        if not rows:
            return None
        votes = Counter((row["kind"], row["value"]) for row in rows)
        threshold = max(1, len(close) // 2)
        winners = [pair for pair, count in votes.items() if count >= threshold]
        if not winners:
            winners = [votes.most_common(1)[0][0]]
        confidence = sum(score for _, score in close) / len(close)
        return ClassificationResult(
            facets=tuple(
                Facet(
                    record_id=record.record_id,
                    kind=kind,
                    value=value,
                    provenance="knn",
                    confidence=confidence,
                )
                for kind, value in winners
            ),
            provenance="knn",
            needs_review=False,
        )

    def _model_layer(self, record: Record) -> ClassificationResult | None:
        if self.model is None:
            return None
        try:
            raw = self.model.label(record.title, record.body)
        except ModelUnavailableError:
            return None
        domains = _valid_labels(raw.get("domains"))
        tags = _valid_labels(raw.get("tags"))
        if not domains and not tags:
            return None
        facets = [
            Facet(
                record_id=record.record_id,
                kind="domain",
                value=value,
                provenance="model",
                confidence=0.6,
            )
            for value in domains
        ] + [
            Facet(
                record_id=record.record_id,
                kind="tag",
                value=value,
                provenance="model",
                confidence=0.6,
            )
            for value in tags
        ]
        return ClassificationResult(
            facets=tuple(facets), provenance="model", needs_review=False
        )
