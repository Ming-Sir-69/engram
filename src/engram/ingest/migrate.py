"""把 Second Brain 的 Markdown 一次性迁入 SQLite。

迁移是**方向的翻转**：此前 Markdown 是事实源、数据库是索引；迁移之后
SQLite 是事实源，Markdown 变成可再生的导出视图。因此这一步必须无损——
行号、标题路径和已有的人工分类都要落库，否则翻转之后再也补不回来。

幂等靠内容哈希：同样的正文重跑只会命中已有记录，不会产生副本。这让迁移
可以先 `--dry-run` 看清楚，再真跑，中断后也能直接重跑。

源文件严格只读。本模块只打开 `SOURCE_FILES` 里的四个文件，不写、不改、
不重命名。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from engram.domain import Facet, RecordDraft, content_hash_for
from engram.errors import InvalidInputError
from engram.ingest.markdown import SourceEntry, parse_markdown
from engram.ingest.taxonomy import SEED_CONFIDENCE, SeedTaxonomy, seed_labels
from engram.repository import RecordRepository

SOURCE_FILES = ("inbox.md", "ideas.md", "knowledge.md", "projects.md")
_PROJECT_FILE = "projects.md"
_REFERENCE_TAG = "external-source"
_SOURCE_AGENT = "migration"


@dataclass(frozen=True, slots=True)
class MigrationReport:
    scanned: int
    created: int
    existing: int
    seeded: int
    unseeded: int

    def to_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "created": self.created,
            "existing": self.existing,
            "seeded": self.seeded,
            "unseeded": self.unseeded,
        }


def _record_type(entry: SourceEntry, tags: tuple[str, ...]) -> str:
    if entry.source_file == _PROJECT_FILE:
        return "project"
    if _REFERENCE_TAG in tags:
        return "reference"
    return "note"


def _attributes(entry: SourceEntry) -> dict[str, object]:
    attributes: dict[str, object] = {
        "source_file": entry.source_file,
        "start_line": entry.start_line,
        "end_line": entry.end_line,
        "heading_path": list(entry.heading_path),
    }
    if entry.recorded_at is not None:
        attributes["recorded_at"] = entry.recorded_at
    return attributes


def _read_entries(source_dir: Path) -> list[SourceEntry]:
    missing = [name for name in SOURCE_FILES if not (source_dir / name).is_file()]
    if missing:
        # 静默跳过会产出"看起来成功"的半截迁移，比直接失败危险得多。
        raise InvalidInputError(
            "source files are missing",
            context={"source_dir": str(source_dir), "missing": missing},
        )
    entries: list[SourceEntry] = []
    for name in SOURCE_FILES:
        text = (source_dir / name).read_text(encoding="utf-8")
        entries.extend(parse_markdown(text, source_file=name))
    return entries


def migrate_from_markdown(
    *,
    source_dir: Path,
    repository: RecordRepository,
    taxonomy: SeedTaxonomy | None = None,
    dry_run: bool = False,
) -> MigrationReport:
    entries = _read_entries(Path(source_dir))
    created = existing = seeded = 0

    for entry in entries:
        domains, tags = seed_labels(entry.heading_path, taxonomy)
        if domains or tags:
            seeded += 1
        if dry_run:
            continue

        digest = content_hash_for(entry.title, entry.body.strip())
        known = repository.connection.execute(
            "SELECT 1 FROM records WHERE content_hash = ?", (digest,)
        ).fetchone()
        record = repository.create(
            RecordDraft(
                title=entry.title,
                body=entry.body,
                record_type=_record_type(entry, tags),
                attributes=_attributes(entry),
                source_agent=_SOURCE_AGENT,
            )
        )
        if known is not None:
            existing += 1
            continue
        created += 1

        # 种子标签直接以 rule 来源写入：源文件标题是长期人工维护的分类
        # 结果，置信度高于任何自动推断，写进去之后补全阶段就不会再猜。
        repository.upsert_facets(
            tuple(
                Facet(
                    record_id=record.record_id,
                    kind="domain",
                    value=value,
                    provenance="rule",
                    confidence=SEED_CONFIDENCE,
                )
                for value in domains
            )
            + tuple(
                Facet(
                    record_id=record.record_id,
                    kind="tag",
                    value=value,
                    provenance="rule",
                    confidence=SEED_CONFIDENCE,
                )
                for value in tags
            )
        )

    return MigrationReport(
        scanned=len(entries),
        created=created,
        existing=existing,
        seeded=seeded,
        unseeded=len(entries) - seeded,
    )
