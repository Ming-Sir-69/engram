from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / "second-brain-data"
DEFAULT_EMBEDDING_MODEL = "nomic-embed-text-v2-moe"
DEFAULT_EMBEDDING_DIMENSIONS = 768
DEFAULT_CLASSIFIER_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"


@dataclass(frozen=True, slots=True)
class EngramConfig:
    data_dir: Path
    db_path: Path
    embedding_model: str
    embedding_dimensions: int
    classifier_model: str
    ollama_base_url: str


def load_config(
    *,
    data_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> EngramConfig:
    environment = os.environ if env is None else env
    if data_dir is not None:
        resolved = Path(data_dir)
    elif "ENGRAM_DATA_DIR" in environment:
        resolved = Path(environment["ENGRAM_DATA_DIR"])
    else:
        resolved = DEFAULT_DATA_DIR
    resolved = resolved.expanduser()
    db_path = resolved / "authoritative" / "engram.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return EngramConfig(
        data_dir=resolved,
        db_path=db_path,
        embedding_model=environment.get(
            "ENGRAM_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL
        ),
        embedding_dimensions=int(
            environment.get(
                "ENGRAM_EMBEDDING_DIMENSIONS", DEFAULT_EMBEDDING_DIMENSIONS
            )
        ),
        classifier_model=environment.get(
            "ENGRAM_CLASSIFIER_MODEL", DEFAULT_CLASSIFIER_MODEL
        ),
        ollama_base_url=environment.get(
            "ENGRAM_OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL
        ),
    )
