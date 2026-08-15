"""金标召回评测。

评测存在的意义是**在换嵌入模型、改分词、调融合权重之后，还能证明检索没变差**。
所以它必须锚在跨迁移稳定的 content_hash 上，并且在金标与数据对不上时直接失败——
静默把对不上的条目算成"未命中"，会让一次数据损坏看起来只是分数下降。
"""

from pathlib import Path

import pytest

from engram.bench import load_gold, run_recall
from engram.db import connect
from engram.domain import RecordDraft, content_hash_for
from engram.errors import InvalidInputError
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.search import SearchService

_BODIES = [
    "岗位聚合工具最大的阻碍在数据层，不在页面层。",
    "负荷分级编排方案按高中低负荷分成三级任务。",
    "公开岗位抓取项目正在开发中，优先推进。",
    "阀盖设计用三段圆弧加卡扣，旋钮解锁弹簧压紧。",
    "隧道内的低频噪声可以作为过渡音效。",
]


def _fixture(tmp_path: Path) -> tuple[RecordRepository, SearchService]:
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    repository = RecordRepository(connection)
    for body in _BODIES:
        repository.create(RecordDraft(title=body[:6], body=body))
    return repository, SearchService(connection)


def _gold(tmp_path: Path, queries: list[dict], *, version: int = 2) -> Path:
    import json

    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps(
            {"schema_version": version, "anchor": "content_hash", "queries": queries},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _hash(index: int) -> str:
    body = _BODIES[index]
    return content_hash_for(body[:6], body)


def test_perfect_retrieval_scores_full_recall(tmp_path: Path) -> None:
    _, search = _fixture(tmp_path)
    gold = _gold(
        tmp_path,
        [
            {
                "id": "q1",
                "query": "岗位聚合工具的阻碍",
                "expected_content_hash": _hash(0),
            },
            {
                "id": "q2",
                "query": "负荷分级编排方案",
                "expected_content_hash": _hash(1),
            },
        ],
    )
    report = run_recall(gold=load_gold(gold), search=search, top_k=5)
    assert report.total == 2
    assert report.hits[5] == 2
    assert report.hits[1] == 2
    assert report.misses == ()


def test_missing_expectation_is_reported_by_id(tmp_path: Path) -> None:
    """只给分数不给 id，回归之后没人知道该去看哪条查询。"""
    _, search = _fixture(tmp_path)
    gold = _gold(
        tmp_path,
        [
            {
                "id": "q-miss",
                "query": "隧道内的低频噪声",
                "expected_content_hash": _hash(3),
            }
        ],
    )
    report = run_recall(gold=load_gold(gold), search=search, top_k=5)
    assert report.hits[5] == 0
    assert report.misses == ("q-miss",)


def test_recall_at_three_is_stricter_than_at_five(tmp_path: Path) -> None:
    _, search = _fixture(tmp_path)
    gold = _gold(
        tmp_path,
        [
            {
                "id": "q1",
                "query": "岗位聚合工具的阻碍",
                "expected_content_hash": _hash(0),
            }
        ],
    )
    report = run_recall(gold=load_gold(gold), search=search, top_k=5)
    assert report.hits[1] <= report.hits[3] <= report.hits[5]


def test_v1_gold_file_is_rejected(tmp_path: Path) -> None:
    """v1 锚在行号上，改版后必然错位；接受它等于给出一个假分数。"""
    path = tmp_path / "old.json"
    path.write_text(
        '{"schema_version": 1, "queries": []}',
        encoding="utf-8",
    )
    with pytest.raises(InvalidInputError) as error:
        load_gold(path)
    assert error.value.context["schema_version"] == 1


def test_unknown_expectation_fails_loudly(tmp_path: Path) -> None:
    _, search = _fixture(tmp_path)
    gold = _gold(
        tmp_path,
        [{"id": "q-ghost", "query": "任意查询", "expected_content_hash": "0" * 64}],
    )
    with pytest.raises(InvalidInputError) as error:
        run_recall(gold=load_gold(gold), search=search, top_k=5)
    assert error.value.context["unresolved"] == ["q-ghost"]


def test_report_serialises_rates(tmp_path: Path) -> None:
    _, search = _fixture(tmp_path)
    gold = _gold(
        tmp_path,
        [
            {
                "id": "q1",
                "query": "公开岗位抓取项目",
                "expected_content_hash": _hash(2),
            }
        ],
    )
    payload = run_recall(gold=load_gold(gold), search=search, top_k=5).to_dict()
    assert payload["total"] == 1
    assert payload["recall"]["5"]["hits"] == 1
    assert payload["recall"]["5"]["rate"] == 1.0
    assert payload["mode"] == "keyword"
