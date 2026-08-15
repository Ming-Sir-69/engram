from pathlib import Path

import pytest

from engram.classify import ClassificationResult, Classifier
from engram.db import connect
from engram.domain import Record
from engram.errors import ModelUnavailableError
from engram.migrations import migrate
from engram.vectors import VectorStore


def make_record(record_id: str = "rec_1", **overrides) -> Record:
    defaults: dict[str, object] = {
        "record_id": record_id,
        "record_type": "note",
        "title": "标题",
        "body": "正文",
        "status": "active",
        "attributes": {},
        "projects": (),
        "revision": 1,
        "created_at": "2026-08-15T00:00:00Z",
        "updated_at": "2026-08-15T00:00:00Z",
        "source_agent": "test",
        "content_hash": "hash",
    }
    defaults.update(overrides)
    return Record(**defaults)


@pytest.fixture()
def store(tmp_path: Path) -> VectorStore:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    return VectorStore(connection, dimensions=8)


def _seed_labeled_neighbor(store: VectorStore) -> None:
    store.connection.execute(
        """
        INSERT INTO records(
            record_id, record_type, title, body, status, attributes_json,
            revision, created_at, updated_at, source_agent, content_hash
        ) VALUES ('rec_old','note','旧','内容','active','{}',1,'t','t','x','h1')
        """
    )
    store.connection.execute(
        "INSERT INTO facets(record_id, kind, value, provenance, confidence) "
        "VALUES ('rec_old','domain','ie-engineering','human',1.0)"
    )
    store.put(
        "rec_old",
        [1.0, 0, 0, 0, 0, 0, 0, 0],
        model="m",
        dimensions=8,
        generation="g1",
        input_hash="h1",
    )


def test_rule_layer_detects_reference_by_url(store: VectorStore) -> None:
    classifier = Classifier(store=store, model=None)
    record = make_record(body="参考 https://arxiv.org/abs/2202.04887 很有用")
    result = classifier.classify(record, vector=None)
    assert result.provenance == "rule"
    assert ("tag", "external-source") in {(f.kind, f.value) for f in result.facets}


def test_rule_layer_detects_project_type(store: VectorStore) -> None:
    classifier = Classifier(store=store, model=None)
    result = classifier.classify(make_record(record_type="project"), vector=None)
    assert result.provenance == "rule"
    assert ("tag", "project-status") in {(f.kind, f.value) for f in result.facets}


def test_knn_layer_inherits_neighbor_labels(store: VectorStore) -> None:
    _seed_labeled_neighbor(store)
    classifier = Classifier(store=store, model=None, knn_threshold=0.4)
    result = classifier.classify(make_record(), vector=[0.99, 0.14, 0, 0, 0, 0, 0, 0])
    assert result.provenance == "knn"
    assert ("domain", "ie-engineering") in {(f.kind, f.value) for f in result.facets}


def test_knn_is_skipped_when_no_close_neighbor(store: VectorStore) -> None:
    _seed_labeled_neighbor(store)

    class StubModel:
        def label(self, title: str, body: str) -> dict[str, list[str]]:
            return {"domains": ["ai-engineering"], "tags": []}

    classifier = Classifier(store=store, model=StubModel(), knn_threshold=0.99)
    result = classifier.classify(make_record(), vector=[0, 0, 0, 0, 0, 0, 0, 1.0])
    assert result.provenance == "model"


def test_model_layer_used_when_no_neighbor(store: VectorStore) -> None:
    class StubModel:
        def label(self, title: str, body: str) -> dict[str, list[str]]:
            return {"domains": ["ai-engineering"], "tags": ["agent"]}

    classifier = Classifier(store=store, model=StubModel())
    result = classifier.classify(make_record(), vector=[0.0] * 8)
    assert result.provenance == "model"
    assert ("domain", "ai-engineering") in {(f.kind, f.value) for f in result.facets}


def test_model_unavailable_falls_back_to_default(store: VectorStore) -> None:
    class BrokenModel:
        def label(self, title: str, body: str) -> dict[str, list[str]]:
            raise ModelUnavailableError("ollama down")

    classifier = Classifier(store=store, model=BrokenModel())
    result = classifier.classify(make_record(), vector=[0.0] * 8)
    assert result.provenance == "default"
    assert result.needs_review is True


def test_illegal_model_output_is_rejected_not_retried(store: VectorStore) -> None:
    class NoisyModel:
        def label(self, title: str, body: str) -> dict[str, list[str]]:
            return {"domains": ["x" * 200], "tags": ["带中文的标签"]}

    classifier = Classifier(store=store, model=NoisyModel())
    result = classifier.classify(make_record(), vector=[0.0] * 8)
    assert result.provenance == "default"
    assert result.needs_review is True


def test_result_is_immutable(store: VectorStore) -> None:
    classifier = Classifier(store=store, model=None)
    result = classifier.classify(make_record(), vector=None)
    assert isinstance(result, ClassificationResult)
    with pytest.raises(AttributeError):
        result.provenance = "human"  # type: ignore[misc]
