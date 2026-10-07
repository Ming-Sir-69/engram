from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATA_DIR = Path.home() / "second-brain-data"
DEFAULT_SOURCE_DIR = Path.home() / ".claude" / "rules" / "second-brain"
DEFAULT_EMBEDDING_MODEL = "Qwen3-Embedding-0.6B"
DEFAULT_EMBEDDING_DIMENSIONS = 1024
DEFAULT_CLASSIFIER_MODEL = "Qwen3-1.7B-4bit"


@dataclass(frozen=True, slots=True)
class EngramConfig:
    data_dir: Path
    db_path: Path
    source_dir: Path
    export_dir: Path | None
    index_path: Path | None
    seed_taxonomy_path: Path
    embedding_model: str
    embedding_dimensions: int
    classifier_model: str
    usage_db_path: Path
    feedback_db_path: Path
    usage_telemetry_enabled: bool

    def model_path(self, name: str) -> Path:
        path = Path(name).expanduser()
        return path if path.is_absolute() else self.data_dir / "models" / path


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
        # 源目录只被迁移和导出校验读取，与数据目录分开配置：
        # 两者混在一起就有把派生内容写回源文件的风险。
        source_dir=Path(
            environment.get("ENGRAM_SOURCE_DIR", DEFAULT_SOURCE_DIR)
        ).expanduser(),
        # 默认不设：自动回写会覆盖目标目录，必须由用户显式指定去处，
        # 不能靠一个猜出来的默认值去写别人的文件。
        export_dir=(
            Path(environment["ENGRAM_EXPORT_DIR"]).expanduser()
            if environment.get("ENGRAM_EXPORT_DIR")
            else None
        ),
        # 与全文导出分开配置：索引适合放进调用方每次都会读到的地方，
        # 全文则不适合——两者的去处天然不同。
        index_path=(
            Path(environment["ENGRAM_INDEX_PATH"]).expanduser()
            if environment.get("ENGRAM_INDEX_PATH")
            else None
        ),
        # 标题映射表是各人自己的分类习惯，属于配置而非代码：
        # 内置一份既对别人无效，也等于把私人笔记的目录结构发布出去。
        seed_taxonomy_path=Path(
            environment.get(
                "ENGRAM_SEED_TAXONOMY", resolved / "config" / "seed_taxonomy.json"
            )
        ).expanduser(),
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
        # 使用统计是可删除的运行数据，不进入 authoritative/、导出或备份。
        # 路径固定在 data_dir 下，避免被误配到仓库或同步目录。
        usage_db_path=resolved / "runtime" / "usage.sqlite3",
        # 反馈与逐次运行观测同样是旁路数据，不进入正式知识库。
        feedback_db_path=resolved / "runtime" / "feedback.sqlite3",
        usage_telemetry_enabled=environment.get("ENGRAM_USAGE_TELEMETRY") != "0",
    )
