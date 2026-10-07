"""Public-copy policy tests. All values and data files here are synthetic."""
import json

import pytest

from engram import run_observer
from engram.mcp.tools import tool_descriptors


def test_public_distribution_has_no_implicit_scheduled_observation(monkeypatch):
    monkeypatch.delenv("ENGRAM_RUN_SCHEDULES", raising=False)
    assert run_observer._engram_schedules() == {"manual": None}


def test_explicit_synthetic_schedule_is_reusable(monkeypatch):
    monkeypatch.setenv("ENGRAM_RUN_SCHEDULES", json.dumps({"synthetic_job": [2, 12, 34]}))
    assert run_observer._engram_schedules() == {"synthetic_job": (2, 12, 34), "manual": None}


@pytest.mark.parametrize("value", ["not-json", '{"x":[7,12,0]}', '{"x":[0,24,0]}'])
def test_invalid_schedule_is_rejected(monkeypatch, value):
    monkeypatch.setenv("ENGRAM_RUN_SCHEDULES", value)
    with pytest.raises(ValueError):
        run_observer._engram_schedules()


def test_deployment_only_tools_are_hidden_by_default(monkeypatch):
    monkeypatch.delenv("ENGRAM_AUTONOMOUS_MAINTENANCE", raising=False)
    monkeypatch.delenv("ENGRAM_CLOUD_SOURCE_MAINTENANCE", raising=False)
    names = {tool["name"] for tool in tool_descriptors()}
    assert "source_improvement" not in names
    assert "maintain" not in names
    assert {"remember", "recall", "get", "status"} <= names


def test_public_icon_is_valid_svg_and_contains_no_embedded_bitmap(tmp_path):
    import xml.etree.ElementTree as ET

    from starlette.testclient import TestClient

    from engram.mcp.remote import create_app

    with TestClient(create_app(data_dir=tmp_path / "synthetic-icon-data", offline=True,
                               token="synthetic-icon-token-0000000000000000000"), base_url="http://127.0.0.1") as client:
        response = client.get("/icon.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert ET.fromstring(response.content).tag == "{http://www.w3.org/2000/svg}svg"
    assert b"data:image" not in response.content


def test_candidate_reader_never_probes_implicit_deployment_paths(monkeypatch):
    from engram import cloud_maintenance

    monkeypatch.delenv("ENGRAM_MAINTENANCE_CANDIDATES_DIR", raising=False)
    def reject_path_probe(*args, **kwargs):
        raise AssertionError("implicit candidate path must not be probed")
    monkeypatch.setattr(cloud_maintenance, "_directory", reject_path_probe)
    assert cloud_maintenance._candidates() == ([], False)


def test_candidate_reader_accepts_explicit_synthetic_directory(tmp_path, monkeypatch):
    from engram import cloud_maintenance

    source = tmp_path / "synthetic-candidates"
    source.mkdir()
    (source / "invented.json").write_text(json.dumps([{
        "record_id": "synthetic-record-001", "reason": "invented sensor fixture",
    }]))
    monkeypatch.setenv("ENGRAM_MAINTENANCE_CANDIDATES_DIR", str(source))
    items, available = cloud_maintenance._candidates()
    assert available and len(items) == 1
    assert items[0]["candidate"]["record_id"] == "synthetic-record-001"
