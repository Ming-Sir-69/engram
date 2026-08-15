import json
from pathlib import Path

import pytest

from engram.cli import main
from engram.db import connect
from engram.domain import RecordDraft
from engram.embedding import DeterministicEmbedder
from engram.migrations import migrate
from engram.repository import RecordRepository
from engram.search import RRF_K, SearchService
from engram.vectors import VectorStore


@pytest.fixture()
def populated(tmp_path: Path):
    connection = connect(tmp_path / "engram.sqlite3")
    migrate(connection)
    counter = iter(f"rec_{index:04d}" for index in range(1, 100))
    repository = RecordRepository(connection, id_factory=lambda: next(counter))
    store = VectorStore(connection, dimensions=64)
    embedder = DeterministicEmbedder(dimensions=64)
    for title, body in [
        ("认知工效学任务管理", "以 A/B/C 三级任务对应高中低负荷"),
        ("人因工程环境因素", "照度 噪音 温度 湿度 对工作人员的影响"),
        ("AI 剪辑候选", "browser-use/video-use 项目 语音转写"),
    ]:
        record = repository.create(RecordDraft(title=title, body=body))
        vector = embedder.embed([f"{title}\n{body}"])[0]
        store.put(
            record.record_id,
            vector,
            model=embedder.model,
            dimensions=64,
            generation="g1",
            input_hash=record.content_hash,
        )
    return connection, store, embedder


def test_vector_search_returns_semantic_neighbor(populated) -> None:
    connection, store, embedder = populated
    search = SearchService(connection, store=store)
    vector = embedder.embed(["负荷 分级 任务"])[0]
    hits = search.vector(vector, limit=3)
    assert hits
    assert hits[0].vector_score > 0


def test_hybrid_merges_both_channels(populated) -> None:
    connection, store, embedder = populated
    search = SearchService(connection, store=store)
    vector = embedder.embed(["认知工效学"])[0]
    hits = search.hybrid("认知工效学", vector, limit=3)
    assert hits[0].title == "认知工效学任务管理"
    assert hits[0].keyword_score > 0
    assert hits[0].vector_score > 0
    assert RRF_K == 60


def test_hybrid_deduplicates_by_record(populated) -> None:
    connection, store, embedder = populated
    search = SearchService(connection, store=store)
    vector = embedder.embed(["人因工程"])[0]
    hits = search.hybrid("人因工程", vector, limit=5)
    assert len({hit.record_id for hit in hits}) == len(hits)


def test_hybrid_surfaces_vector_only_match(populated) -> None:
    """关键词无法命中时，向量通道仍应把语义相近的记录带出来。"""
    connection, store, embedder = populated
    search = SearchService(connection, store=store)
    vector = embedder.embed(["照度 噪音 温度"])[0]
    hits = search.hybrid("照度 噪音 温度", vector, limit=3)
    assert any(hit.title == "人因工程环境因素" for hit in hits)


def test_cli_drain_reports_counts(capsys, tmp_path: Path) -> None:
    main(
        ["--data-dir", str(tmp_path), "record", "create", "--title", "t", "--body", "b"]
    )
    capsys.readouterr()
    code = main(["--data-dir", str(tmp_path), "index", "drain", "--offline"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["processed"] >= 1
    assert payload["succeeded"] >= 1


def test_cli_status_includes_vector_state(capsys, tmp_path: Path) -> None:
    main(
        ["--data-dir", str(tmp_path), "record", "create", "--title", "t", "--body", "b"]
    )
    main(["--data-dir", str(tmp_path), "index", "drain", "--offline"])
    capsys.readouterr()
    main(["--data-dir", str(tmp_path), "status"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["vectors"] >= 1
    assert payload["backlog"]["pending"] == 0


def test_cli_hybrid_search_end_to_end(capsys, tmp_path: Path) -> None:
    main(
        [
            "--data-dir",
            str(tmp_path),
            "record",
            "create",
            "--title",
            "认知工效学",
            "--body",
            "任务负荷分级",
        ]
    )
    main(["--data-dir", str(tmp_path), "index", "drain", "--offline"])
    capsys.readouterr()
    code = main(
        [
            "--data-dir",
            str(tmp_path),
            "search",
            "认知工效学",
            "--mode",
            "hybrid",
            "--offline",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["results"][0]["title"] == "认知工效学"


def test_write_path_loads_no_model_modules(tmp_path: Path) -> None:
    """写入路径必须不加载嵌入/网络模块——模型不可用不能影响写入。"""
    import subprocess
    import sys

    script = (
        "import sys, engram.cli;"
        "risky=[m for m in sys.modules "
        "if 'ollama' in m.lower() or m.endswith('engram.embedding') "
        "or m in ('urllib.request','socket')];"
        "print(risky)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "[]"
