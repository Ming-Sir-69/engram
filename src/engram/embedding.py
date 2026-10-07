from __future__ import annotations

import json
import math
import re
import struct
import threading
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from engram.errors import ModelUnavailableError


class Embedder(Protocol):
    model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def to_blob(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def from_blob(data: bytes) -> list[float]:
    return list(struct.unpack(f"{len(data) // 4}f", data))


@lru_cache(maxsize=2)
def _load_mlx_embedding(path: str):
    from mlx_embeddings import load

    return load(path)


_mlx_inference_lock = threading.Lock()


class MLXEmbedder:
    """Apple Silicon 上按需加载的本地 Qwen3 embedding。"""

    QUERY_INSTRUCTION = (
        "Instruct: Retrieve relevant passages from a personal knowledge base "
        "for the user's question.\nQuery: "
    )

    def __init__(
        self,
        *,
        model_path: Path,
        model: str = "Qwen/Qwen3-Embedding-0.6B",
        dimensions: int = 1024,
    ) -> None:
        self.model_path = Path(model_path)
        self.model = model
        self.dimensions = dimensions

    def embed_query(self, text: str) -> list[float]:
        return self.embed([self.QUERY_INSTRUCTION + text])[0]

    def embed_queries(self, texts: list[str]) -> list[list[float]]:
        return self.embed([self.QUERY_INSTRUCTION + text for text in texts])

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not (self.model_path / "model.safetensors").is_file():
            raise ModelUnavailableError(
                f"local MLX embedding model is missing: {self.model_path}"
            )
        try:
            import mlx.core as mx

            with _mlx_inference_lock:
                model, tokenizer = _load_mlx_embedding(str(self.model_path))
                vectors = []
                for start in range(0, len(texts), 4):
                    inputs = tokenizer(
                        texts[start : start + 4],
                        padding=True,
                        truncation=True,
                        max_length=4096,
                        return_tensors="np",
                    )
                    outputs = model(
                        mx.array(inputs["input_ids"]),
                        attention_mask=mx.array(inputs["attention_mask"]),
                    )
                    result = outputs.text_embeds
                    mx.eval(result)
                    vectors.extend(result.tolist())
        except (ImportError, OSError) as exc:
            raise ModelUnavailableError(
                f"local MLX embedding model unavailable: {type(exc).__name__}"
            ) from exc
        if len(vectors) != len(texts) or any(
            len(vector) != self.dimensions for vector in vectors
        ):
            raise ValueError("MLX embedding dimensions or batch size mismatch")
        return vectors


class SharedMLXEmbedder:
    """Use the shared loopback model when available, with local fallback."""

    def __init__(
        self,
        local: MLXEmbedder,
        *,
        base_url: str = "http://127.0.0.1:8772",
        allow_local_fallback: bool = True,
        timeout: float = 120,
    ):
        self.local = local
        self.model = local.model
        self.dimensions = local.dimensions
        self.base_url = base_url.rstrip("/")
        self.allow_local_fallback = allow_local_fallback
        self.timeout = timeout

    def _call(self, texts: list[str], kind: str) -> list[list[float]]:
        if not texts:
            return []
        request = Request(
            self.base_url + "/embed",
            data=json.dumps({"texts": texts, "kind": kind}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                vectors = json.load(response).get("vectors")
        except HTTPError as exc:
            raise ModelUnavailableError(
                f"shared MLX embedding returned HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, ConnectionRefusedError):
                if not self.allow_local_fallback:
                    raise ModelUnavailableError(
                        "shared MLX embedding unavailable"
                    ) from exc
                return (
                    self.local.embed_queries(texts)
                    if kind == "query"
                    else self.local.embed(texts)
                )
            raise ModelUnavailableError(
                f"shared MLX embedding unavailable: {type(exc.reason).__name__}"
            ) from exc
        except TimeoutError as exc:
            raise ModelUnavailableError("shared MLX embedding timed out") from exc
        if (
            not isinstance(vectors, list)
            or len(vectors) != len(texts)
            or any(len(vector) != self.dimensions for vector in vectors)
        ):
            raise ValueError("shared MLX embedding returned invalid vectors")
        return vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._call(texts, "document")

    def embed_query(self, text: str) -> list[float]:
        return self._call([text], "query")[0]


class DeterministicEmbedder:
    """离线测试用，同一输入永远得到同一向量。"""

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
