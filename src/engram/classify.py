from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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


LabelTransport = Callable[[str, dict[str, object]], dict[str, object]]


def _is_local(url: str) -> bool:
    return urlparse(url).hostname in {"127.0.0.1", "localhost", "::1"}


def _chat_transport(url: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=120) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result if isinstance(result, dict) else {}


class OllamaLabelModel:
    """本地小模型分类器。

    只在 kNN 找不到足够近邻时才被调用，因此调用量会随知识库增长而下降。
    模型不可达抛 `ModelUnavailableError`（可重试）；输出不合法则返回空标签，
    由调用方降级到默认层——同样的输入不会得到不同的坏输出，重试无意义。
    """

    def __init__(
        self,
        *,
        model: str = "qwen3.5:4b",
        base_url: str = "http://127.0.0.1:11434",
        transport: LabelTransport | None = None,
    ) -> None:
        if not _is_local(base_url):
            raise ValueError("classifier base_url must be loopback-only")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._transport = transport or _chat_transport

    def label(self, title: str, body: str) -> dict[str, list[str]]:
        prompt = _PROMPT.format(
            vocabulary="\n".join(f"- {name}" for name in DOMAIN_VOCABULARY),
            title=title[:200],
            body=body[:1500],
        )
        try:
            response = self._transport(
                f"{self.base_url}/api/chat",
                {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "format": "json",
                    "stream": False,
                    # 必须关闭思考链：qwen3.5:4b 这类模型开启后会为一次分类
                    # 生成上万字推理，实测 170.7s 对 2.1s（81 倍），且超长
                    # 思考会让最终 content 更容易跑偏成空值。分类是封闭集合
                    # 上的选择题，不需要推理链。不支持该参数的服务端会忽略它。
                    "think": False,
                    "options": {"temperature": 0},
                },
            )
        except OSError as exc:
            raise ModelUnavailableError(
                f"ollama chat endpoint unreachable: {type(exc).__name__}"
            ) from exc
        content = response.get("message", {})
        text = content.get("content", "") if isinstance(content, dict) else ""
        try:
            parsed = json.loads(text)
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
