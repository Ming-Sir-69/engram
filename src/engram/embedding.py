from __future__ import annotations

import json
import math
import re
import struct
from collections.abc import Callable
from hashlib import sha256
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from engram.errors import ModelUnavailableError

Transport = Callable[[str, dict[str, object]], dict[str, object]]


class Embedder(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def to_blob(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def from_blob(data: bytes) -> list[float]:
    return list(struct.unpack(f"{len(data) // 4}f", data))


def _is_local(url: str) -> bool:
    return urlparse(url).hostname in {"127.0.0.1", "localhost", "::1"}


def _http_transport(url: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=120) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        # 刻意使用 ValueError 而非 TypeError：补全服务据此把「响应格式非法」
        # 判为确定性失败并停止重试，同样的请求不会得到不同的坏响应。
        raise ValueError("ollama returned a non-object response")  # noqa: TRY004
    return result


class OllamaEmbedder:
    def __init__(
        self,
        *,
        model: str = "nomic-embed-text-v2-moe",
        dimensions: int = 768,
        base_url: str = "http://127.0.0.1:11434",
        transport: Transport | None = None,
    ) -> None:
        if not _is_local(base_url):
            raise ValueError("embedding base_url must be loopback-only")
        self.model = model
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/")
        self._transport = transport or _http_transport

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = self._transport(
                f"{self.base_url}/api/embed",
                {"model": self.model, "input": texts},
            )
        except OSError as exc:
            raise ModelUnavailableError(
                f"ollama embedding endpoint unreachable: {type(exc).__name__}"
            ) from exc
        raw = response.get("embeddings")
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise ValueError("ollama returned an invalid embeddings batch")
        vectors: list[list[float]] = []
        for item in raw:
            vector = [float(value) for value in item]
            if len(vector) != self.dimensions:
                raise ValueError(
                    f"embedding dimensions mismatch: expected {self.dimensions}, "
                    f"got {len(vector)}"
                )
            vectors.append(vector)
        return vectors


class DeterministicEmbedder:
    """离线测试用，不依赖 Ollama，同一输入永远得到同一向量。"""

    model = "deterministic-hash-v1"

    def __init__(self, *, dimensions: int = 256) -> None:
        if dimensions < 8:
            raise ValueError("dimensions must be at least 8")
        self.dimensions = dimensions

    @staticmethod
    def _tokens(text: str) -> list[str]:
        lowered = text.lower()
        chinese = re.findall(r"[㐀-鿿]", lowered)
        bigrams = [
            "".join(chinese[index : index + 2])
            for index in range(max(0, len(chinese) - 1))
        ]
        words = re.findall(r"[a-z0-9][a-z0-9_.-]*", lowered)
        return chinese + bigrams + words

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token in self._tokens(text):
                digest = sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                vector[index] += 1.0 if digest[4] & 1 else -1.0
            norm = math.sqrt(sum(value * value for value in vector))
            if norm == 0:
                vector[0] = 1.0
                norm = 1.0
            vectors.append([value / norm for value in vector])
        return vectors
