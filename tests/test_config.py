from pathlib import Path

import pytest

from engram.config import DEFAULT_DATA_DIR, load_config


def test_default_data_dir_is_outside_any_repository() -> None:
    assert DEFAULT_DATA_DIR == Path.home() / "second-brain-data"
    assert ".git" not in str(DEFAULT_DATA_DIR)


def test_env_overrides_default(tmp_path: Path) -> None:
    config = load_config(env={"ENGRAM_DATA_DIR": str(tmp_path)})
    assert config.data_dir == tmp_path
    assert config.db_path == tmp_path / "authoritative" / "engram.sqlite3"


def test_explicit_argument_wins_over_env(tmp_path: Path) -> None:
    other = tmp_path / "explicit"
    config = load_config(data_dir=other, env={"ENGRAM_DATA_DIR": str(tmp_path)})
    assert config.data_dir == other


def test_load_config_creates_directories(tmp_path: Path) -> None:
    config = load_config(data_dir=tmp_path / "fresh")
    assert config.db_path.parent.is_dir()


def test_config_is_frozen(tmp_path: Path) -> None:
    config = load_config(data_dir=tmp_path)
    with pytest.raises(AttributeError):
        config.data_dir = tmp_path  # type: ignore[misc]
