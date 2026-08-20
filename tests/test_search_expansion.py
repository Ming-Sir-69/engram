"""hybrid 的一跳链接扩展：RRF 融合后沿高质量边捞回近邻。

边来自写入期的向量 kNN，本身不带关键词命中；扩展条目因此
keyword_score=0、vector_score=边分，语义诚实且压不过双通道命中。
"""

from pathlib import Path

import pytest

from engram.db import connect
from engram.domain import RecordDraft
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.search import SearchService


@pytest.fixture()
def context(tmp_path: Path):
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    counter = iter(f"rec_{index:04d}" for index in range(1, 100))
    repository = RecordRepository(connection, id_factory=lambda: next(counter))
    return repository, SearchService(connection)


def _link(repository: RecordRepository, source: str, target: str, score: float) -> None:
    repository.connection.execute(
        "INSERT INTO record_links(source_id, target_id, relation, score, provenance) "
        "VALUES (?, ?, 'related_to', ?, 'knn')",
        (source, target, score),
    )


def test_hybrid_expands_along_high_score_edges(context) -> None:
    repository, search = context
    hit = repository.create(RecordDraft(title="直接命中", body="独特词甲 内容"))
    neighbor = repository.create(RecordDraft(title="邻居", body="平平无奇的正文"))
    _link(repository, hit.record_id, neighbor.record_id, 0.8)

    results = search.hybrid("独特词甲", [], limit=5)

    by_id = {item.record_id: item for item in results}
    assert hit.record_id in by_id
    assert neighbor.record_id in by_id
    expanded = by_id[neighbor.record_id]
    assert expanded.keyword_score == 0.0
    assert expanded.vector_score == 0.8
    assert expanded.score > 0.0
    # 同一记录不因扩展重复出现
    assert len([item for item in results if item.record_id == neighbor.record_id]) == 1


def test_expansion_contribution_stays_below_direct_hits(context) -> None:
    repository, search = context
    hit = repository.create(RecordDraft(title="直接命中", body="独特词乙 内容"))
    neighbor = repository.create(RecordDraft(title="邻居", body="无关正文"))
    _link(repository, hit.record_id, neighbor.record_id, 0.9)

    results = search.hybrid("独特词乙", [], limit=5)

    assert results[0].record_id == hit.record_id
    by_id = {item.record_id: item for item in results}
    assert by_id[neighbor.record_id].score < by_id[hit.record_id].score


def test_low_score_edges_are_not_expanded(context) -> None:
    repository, search = context
    hit = repository.create(RecordDraft(title="直接命中", body="独特词丙 内容"))
    neighbor = repository.create(RecordDraft(title="弱邻居", body="无关正文"))
    _link(repository, hit.record_id, neighbor.record_id, 0.3)

    results = search.hybrid("独特词丙", [], limit=5)

    assert [item.record_id for item in results] == [hit.record_id]


def test_expansion_uses_at_most_three_edges_per_seed(context) -> None:
    repository, search = context
    hit = repository.create(RecordDraft(title="直接命中", body="独特词丁 内容"))
    for index in range(5):
        neighbor = repository.create(
            RecordDraft(title=f"邻居{index}", body=f"无关正文 {index}")
        )
        _link(repository, hit.record_id, neighbor.record_id, 0.9 - index * 0.01)

    results = search.hybrid("独特词丁", [], limit=10)

    expanded = [item for item in results if item.record_id != hit.record_id]
    assert len(expanded) == 3
    assert [item.title for item in expanded] == ["邻居0", "邻居1", "邻居2"]


def test_results_are_identical_without_edges(context) -> None:
    """无边库的回归保护：扩展不能改变没有链接时的任何结果。"""
    repository, search = context
    first = repository.create(RecordDraft(title="甲", body="独特词戊 内容"))
    repository.create(RecordDraft(title="乙", body="独特词戊 另一段"))

    results = search.hybrid("独特词戊", [], limit=5)

    assert len(results) == 2
    assert results[0].record_id == first.record_id
    assert all(item.keyword_score > 0 for item in results)
    assert all(item.vector_score == 0 for item in results)


def test_expansion_skips_inactive_neighbors(context) -> None:
    repository, search = context
    hit = repository.create(RecordDraft(title="直接命中", body="独特词己 内容"))
    neighbor = repository.create(RecordDraft(title="邻居", body="无关正文"))
    _link(repository, hit.record_id, neighbor.record_id, 0.8)
    repository.connection.execute(
        "UPDATE records SET status = 'archived' WHERE record_id = ?",
        (neighbor.record_id,),
    )

    results = search.hybrid("独特词己", [], limit=5)

    assert [item.record_id for item in results] == [hit.record_id]
