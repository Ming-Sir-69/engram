import pytest

from engram.embedding import (
    DeterministicEmbedder,
    MLXEmbedder,
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


def test_empty_batch_returns_empty() -> None:
    embedder = DeterministicEmbedder(dimensions=64)
    assert embedder.embed([]) == []


def test_mlx_requires_local_model(tmp_path) -> None:
    embedder = MLXEmbedder(model_path=tmp_path / "missing")
    with pytest.raises(ModelUnavailableError, match="missing"):
        embedder.embed(["知识库检索"])


def test_mlx_query_uses_retrieval_instruction(tmp_path) -> None:
    embedder = MLXEmbedder(model_path=tmp_path)
    captured = []
    embedder.embed = lambda texts: captured.extend(texts) or [[0.0] * 1024]
    embedder.embed_query("负荷分级")
    assert captured[0].startswith("Instruct: Retrieve relevant passages")
    assert captured[0].endswith("Query: 负荷分级")
