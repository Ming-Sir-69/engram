"""已有可信标签的记录不应被重新猜测。

迁移把源文件标题收割成 `rule` 来源的种子标签；补全阶段若再跑一遍 kNN 和
模型，既浪费算力，又会用低置信度结果覆盖高置信度标注。
"""

from pathlib import Path

from engram.classify import Classifier
from engram.db import connect
from engram.domain import Facet, RecordDraft
from engram.embedding import DeterministicEmbedder
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.vectors import VectorStore


class _ExplodingModel:
    def label(self, title: str, body: str) -> dict[str, list[str]]:
        raise AssertionError("已有种子标签时不应调用模型")


def _setup(tmp_path: Path):
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    repository = RecordRepository(connection)
    store = VectorStore(connection, dimensions=64)
    return connection, repository, store


def test_rule_facets_short_circuit_the_chain(tmp_path: Path) -> None:
    _, repository, store = _setup(tmp_path)
    record = repository.create(RecordDraft(title="阀盖卡扣", body="四个圆弧弹簧锁定"))
    repository.upsert_facets(
        (
            Facet(
                record_id=record.record_id,
                kind="domain",
                value="product-design",
                provenance="rule",
                confidence=0.9,
            ),
        )
    )
    vector = DeterministicEmbedder(dimensions=64).embed(["x"])[0]
    result = Classifier(store=store, model=_ExplodingModel()).classify(record, vector)
    assert result.provenance == "rule"
    assert result.needs_review is False
    assert [(f.kind, f.value) for f in result.facets] == [("domain", "product-design")]


def test_human_facets_are_also_respected(tmp_path: Path) -> None:
    _, repository, store = _setup(tmp_path)
    record = repository.create(RecordDraft(title="t", body="b"))
    repository.upsert_facets(
        (
            Facet(
                record_id=record.record_id,
                kind="tag",
                value="hand-picked",
                provenance="human",
                confidence=1.0,
            ),
        )
    )
    vector = DeterministicEmbedder(dimensions=64).embed(["x"])[0]
    result = Classifier(store=store, model=_ExplodingModel()).classify(record, vector)
    assert result.provenance == "rule"
    assert [f.value for f in result.facets] == ["hand-picked"]


def test_model_facets_do_not_short_circuit(tmp_path: Path) -> None:
    """模型给的标签置信度不足以豁免后续修正。"""
    _, repository, store = _setup(tmp_path)
    record = repository.create(RecordDraft(title="t", body="b"))
    repository.upsert_facets(
        (
            Facet(
                record_id=record.record_id,
                kind="tag",
                value="guessed",
                provenance="model",
                confidence=0.6,
            ),
        )
    )

    class _Model:
        def label(self, title: str, body: str) -> dict[str, list[str]]:
            return {"domains": ["tooling"], "tags": []}

    result = Classifier(store=store, model=_Model()).classify(record, None)
    assert result.provenance == "model"


def test_upsert_facets_is_idempotent(tmp_path: Path) -> None:
    connection, repository, _ = _setup(tmp_path)
    record = repository.create(RecordDraft(title="t", body="b"))
    facet = Facet(
        record_id=record.record_id,
        kind="tag",
        value="x",
        provenance="rule",
        confidence=0.9,
    )
    repository.upsert_facets((facet,))
    repository.upsert_facets((facet,))
    count = connection.execute(
        "SELECT COUNT(*) FROM facets WHERE record_id = ?", (record.record_id,)
    ).fetchone()[0]
    assert count == 1
