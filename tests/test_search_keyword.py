from pathlib import Path

import pytest

from engram.db import connect
from engram.domain import RecordDraft
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.search import SearchService


@pytest.fixture()
def service(tmp_path: Path) -> tuple[RecordRepository, SearchService]:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    counter = iter(f"rec_{index:04d}" for index in range(1, 1000))
    repository = RecordRepository(
        connection,
        id_factory=lambda: next(counter),
        clock=lambda: "2026-08-15T00:00:00Z",
    )
    return repository, SearchService(connection)


def test_chinese_query_matches(service) -> None:
    repository, search = service
    record = repository.create(
        RecordDraft(title="认知工效学任务管理", body="以 A/B/C 三级任务对应负荷")
    )
    hits = search.keyword("认知工效", limit=5)
    assert hits[0].record_id == record.record_id
    assert hits[0].keyword_score > 0


def test_identifier_query_matches(service) -> None:
    repository, search = service
    record = repository.create(
        RecordDraft(title="AI 剪辑候选", body="项目 browser-use/video-use 值得关注")
    )
    hits = search.keyword("browser-use/video-use", limit=5)
    assert hits[0].record_id == record.record_id


def test_unrelated_query_returns_nothing(service) -> None:
    repository, search = service
    repository.create(RecordDraft(title="认知工效学", body="人因"))
    assert search.keyword("量子色动力学", limit=5) == []


def test_limit_is_respected(service) -> None:
    repository, search = service
    for index in range(8):
        repository.create(RecordDraft(title=f"人因工程 {index}", body="标准时间"))
    assert len(search.keyword("人因", limit=3)) == 3


def test_excerpt_is_bounded(service) -> None:
    repository, search = service
    repository.create(RecordDraft(title="长文", body="人因" * 500))
    hit = search.keyword("人因", limit=1)[0]
    assert len(hit.excerpt) <= 200


def test_empty_query_returns_nothing(service) -> None:
    repository, search = service
    repository.create(RecordDraft(title="t", body="b"))
    assert search.keyword("   ", limit=5) == []
