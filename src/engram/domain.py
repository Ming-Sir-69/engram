from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256

RECORD_TYPES = frozenset({"note", "reference", "project"})
RECORD_STATUSES = frozenset({"active", "archived"})
FACET_KINDS = frozenset({"domain", "tag"})
PROVENANCES = frozenset({"rule", "knn", "model", "default", "human"})


def content_hash_for(title: str, body: str) -> str:
    return sha256(f"{title}\n{body}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RecordDraft:
    title: str
    body: str
    record_type: str = "note"
    attributes: Mapping[str, object] = field(default_factory=dict)
    projects: tuple[str, ...] = ()
    source_agent: str = "unknown"


@dataclass(frozen=True, slots=True)
class Record:
    record_id: str
    record_type: str
    title: str
    body: str
    status: str
    attributes: Mapping[str, object]
    projects: tuple[str, ...]
    revision: int
    created_at: str
    updated_at: str
    source_agent: str
    content_hash: str
    archived_at: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "record_type": self.record_type,
            "title": self.title,
            "body": self.body,
            "status": self.status,
            "attributes": dict(self.attributes),
            "projects": list(self.projects),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_agent": self.source_agent,
            "content_hash": self.content_hash,
            "archived_at": self.archived_at,
        }


@dataclass(frozen=True, slots=True)
class Facet:
    record_id: str
    kind: str
    value: str
    provenance: str
    confidence: float = 0.0
    locked: bool = False
