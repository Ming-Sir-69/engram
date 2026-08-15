import json
from pathlib import Path

from engram.cli import main
from engram.domain import content_hash_for

_BODY = "岗位聚合工具最大的阻碍在数据层，不在页面层。"
_TITLE = "岗位聚合工具"


def _gold(tmp_path: Path, *, schema_version: int = 2) -> Path:
    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": schema_version,
                "anchor": "content_hash",
                "queries": [
                    {
                        "id": "q1",
                        "query": "岗位聚合工具的阻碍",
                        "expected_content_hash": content_hash_for(_TITLE, _BODY),
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _seed(tmp_path: Path, capsys) -> None:
    main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "record",
            "create",
            "--title",
            _TITLE,
            "--body",
            _BODY,
        ]
    )
    capsys.readouterr()


def test_bench_recall_reports_rates(capsys, tmp_path: Path) -> None:
    _seed(tmp_path, capsys)
    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "bench",
            "recall",
            "--gold",
            str(_gold(tmp_path)),
            "--top-k",
            "5",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["recall"]["5"]["hits"] == 1


def test_bench_recall_gate_fails_below_threshold(capsys, tmp_path: Path) -> None:
    """门槛必须由工具判定：靠人读分数的门槛迟早会被"差不多"放过去。"""
    _seed(tmp_path, capsys)
    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "bench",
            "recall",
            "--gold",
            str(_gold(tmp_path)),
            "--min-hits",
            "2",
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["gate"] == {"min_hits": 2, "hits": 1, "passed": False}


def test_bench_recall_rejects_stale_gold(capsys, tmp_path: Path) -> None:
    _seed(tmp_path, capsys)
    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "bench",
            "recall",
            "--gold",
            str(_gold(tmp_path, schema_version=1)),
        ]
    )
    assert code == 65
    problem = json.loads(capsys.readouterr().err)
    assert problem["code"] == "SB-400-INVALID-INPUT"
