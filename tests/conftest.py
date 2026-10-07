"""Isolate process-level home resolution and every Engram path before collection.

HOME/CODEX_HOME environment variables are never replaced. Child business processes
inherit explicit synthetic data/source/auth paths; model downloads remain disabled.
"""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

_SESSION_TEMP = TemporaryDirectory(prefix="engram-public-tests-")
_SESSION_ROOT = Path(_SESSION_TEMP.name).resolve()
_SYNTHETIC_HOME = _SESSION_ROOT / "synthetic-home"
_SYNTHETIC_HOME.mkdir()
_HOME_VIEW = pytest.MonkeyPatch()
_ORIGINAL_EXPANDUSER = os.path.expanduser
_HOME_VIEW.setattr(Path, "home", classmethod(lambda cls: _SYNTHETIC_HOME))


def _synthetic_expanduser(path):
    raw = os.fspath(path)
    if raw == "~":
        return str(_SYNTHETIC_HOME)
    if isinstance(raw, str) and raw.startswith("~/"):
        return str(_SYNTHETIC_HOME / raw[2:])
    return _ORIGINAL_EXPANDUSER(path)


_HOME_VIEW.setattr(os.path, "expanduser", _synthetic_expanduser)
for _name in tuple(os.environ):
    if _name.startswith("ENGRAM_"):
        os.environ.pop(_name, None)
os.environ["ENGRAM_DATA_DIR"] = str(_SESSION_ROOT / "synthetic-session-data")
os.environ["ENGRAM_SOURCE_DIR"] = str(_SESSION_ROOT / "synthetic-session-source")
os.environ["OWNER_TOTP_FILE"] = str(_SESSION_ROOT / "missing-synthetic-totp")
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


@pytest.fixture(autouse=True)
def _isolate_synthetic_runtime(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("ENGRAM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENGRAM_DATA_DIR", str(tmp_path / "synthetic-data"))
    monkeypatch.setenv("ENGRAM_SOURCE_DIR", str(tmp_path / "synthetic-source"))
    monkeypatch.setenv("ENGRAM_OBSERVER_DIR", str(tmp_path / "synthetic-observer"))
    monkeypatch.setenv("ENGRAM_CLAUDE_HOOK_STATE_DIR", str(tmp_path / "synthetic-hook"))
    monkeypatch.setenv("OWNER_TOTP_FILE", str(tmp_path / "missing-synthetic-totp"))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")


def pytest_unconfigure(config):
    _HOME_VIEW.undo()
    _SESSION_TEMP.cleanup()
