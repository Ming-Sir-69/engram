import pytest

from engram.classify import DOMAIN_VOCABULARY, MLXLabelModel
from engram.errors import ModelUnavailableError


def test_mlx_classifier_parses_closed_vocabulary(tmp_path) -> None:
    captured = []
    model = MLXLabelModel(
        model_path=tmp_path,
        runner=lambda prompt: captured.append(prompt)
        or '{"domains":["learning","unknown"],"tags":["reading"]}',
    )
    result = model.label("学习方法", "长期记忆")
    assert result == {"domains": ["learning"], "tags": ["reading"]}
    for domain in DOMAIN_VOCABULARY:
        assert domain in captured[0]


def test_mlx_classifier_handles_invalid_output(tmp_path) -> None:
    model = MLXLabelModel(model_path=tmp_path, runner=lambda _: "invalid")
    assert model.label("标题", "正文") == {"domains": [], "tags": []}


def test_mlx_classifier_requires_local_model(tmp_path) -> None:
    with pytest.raises(ModelUnavailableError, match="missing"):
        MLXLabelModel(model_path=tmp_path).label("标题", "正文")
