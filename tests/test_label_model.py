import json

import pytest

from engram.classify import DOMAIN_VOCABULARY, OllamaLabelModel
from engram.errors import ModelUnavailableError


def _response(payload: dict) -> dict[str, object]:
    return {"message": {"content": json.dumps(payload, ensure_ascii=False)}}


def test_rejects_remote_url() -> None:
    with pytest.raises(ValueError):
        OllamaLabelModel(base_url="http://example.com:11434")


def test_parses_domains_and_tags() -> None:
    def transport(url: str, payload: dict[str, object]) -> dict[str, object]:
        return _response({"domains": ["ie-engineering"], "tags": ["workload"]})

    model = OllamaLabelModel(transport=transport)
    result = model.label("负荷分级", "任务负荷分级")
    assert result["domains"] == ["ie-engineering"]
    assert result["tags"] == ["workload"]


def test_domains_outside_vocabulary_are_dropped() -> None:
    def transport(url: str, payload: dict[str, object]) -> dict[str, object]:
        return _response({"domains": ["ie-engineering", "astrology"], "tags": []})

    model = OllamaLabelModel(transport=transport)
    assert model.label("t", "b")["domains"] == ["ie-engineering"]


def test_unreachable_endpoint_raises_model_unavailable() -> None:
    def transport(url: str, payload: dict[str, object]) -> dict[str, object]:
        raise OSError("connection refused")

    model = OllamaLabelModel(transport=transport)
    with pytest.raises(ModelUnavailableError):
        model.label("t", "b")


def test_non_json_content_yields_empty_labels() -> None:
    def transport(url: str, payload: dict[str, object]) -> dict[str, object]:
        return {"message": {"content": "抱歉，我无法完成这个请求。"}}

    model = OllamaLabelModel(transport=transport)
    assert model.label("t", "b") == {"domains": [], "tags": []}


def test_prompt_contains_closed_vocabulary() -> None:
    captured: dict[str, object] = {}

    def transport(url: str, payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return _response({"domains": [], "tags": []})

    model = OllamaLabelModel(transport=transport)
    model.label("标题", "正文")
    prompt = json.dumps(captured, ensure_ascii=False)
    for domain in DOMAIN_VOCABULARY:
        assert domain in prompt
    assert captured["format"] == "json"
    assert captured["stream"] is False


def test_thinking_is_disabled() -> None:
    """思考链必须关闭：实测开启后单条分类从 2.1s 涨到 170.7s。"""
    captured: dict[str, object] = {}

    def transport(url: str, payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return _response({"domains": [], "tags": []})

    OllamaLabelModel(transport=transport).label("t", "b")
    assert captured["think"] is False
