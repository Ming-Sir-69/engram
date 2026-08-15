from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from engram.domain import Facet, Record
from engram.errors import ModelUnavailableError
from engram.vectors import VectorStore

_URL = re.compile(r"https?://\S+")
_MAX_LABEL_LENGTH = 40
_LABEL_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEFAULT_DOMAIN = "unsorted"


class LabelModel(Protocol):
    def label(self, title: str, body: str) -> dict[str, list[str]]: ...


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
