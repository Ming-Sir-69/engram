import pytest

from engram.embedding import (
    DeterministicEmbedder,
    OllamaEmbedder,
    from_blob,
    to_blob,
)
from engram.errors import ModelUnavailableError


def test_blob_round_trip() -> None:
    vector = [0.5, -0.25, 0.125]
    assert from_blob(to_blob(vector)) == pytest.approx(vector)


def test_deterministic_embedder_is_stable() -> None:
    embedder = DeterministicEmbedder(dimensions=64)
    first = embedder.embed(["负荷分级"])
    second = embedder.embed(["负荷分级"])
    assert first == second
    assert len(first[0]) == 64


def test_deterministic_embedder_separates_topics() -> None:
    embedder = DeterministicEmbedder(dimensions=64)
    vectors = embedder.embed(["负荷分级 人因", "量子色动力学 夸克"])
    dot = sum(a * b for a, b in zip(vectors[0], vectors[1], strict=True))
    assert dot < 0.5


def test_ollama_rejects_remote_url() -> None:
    with pytest.raises(ValueError):
        OllamaEmbedder(base_url="http://example.com:11434")


def test_ollama_transport_failure_becomes_model_unavailable() -> None:
    def broken(url: str, payload: dict[str, object]) -> dict[str, object]:
        raise OSError("connection refused")

    embedder = OllamaEmbedder(transport=broken)
    with pytest.raises(ModelUnavailableError):
        embedder.embed(["x"])


def test_dimension_mismatch_is_permanent_error() -> None:
    def wrong(url: str, payload: dict[str, object]) -> dict[str, object]:
        return {"embeddings": [[0.1, 0.2]]}

    embedder = OllamaEmbedder(dimensions=768, transport=wrong)
    with pytest.raises(ValueError):
        embedder.embed(["x"])


def test_empty_batch_returns_empty() -> None:
    embedder = DeterministicEmbedder(dimensions=64)
    assert embedder.embed([]) == []
