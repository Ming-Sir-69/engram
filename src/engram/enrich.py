from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from engram.classify import Classifier
from engram.db import write_transaction
from engram.embedding import Embedder
from engram.errors import ModelUnavailableError
from engram.links import build_links
from engram.repository import RecordRepository
from engram.vectors import VectorStore

BACKOFF_SECONDS = (60, 300, 1800, 7200, 43200)
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True, slots=True)
class DrainResult:
    processed: int
    succeeded: int
    failed_transient: int
    failed_permanent: int

    def to_dict(self) -> dict[str, int]:
        return {
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed_transient": self.failed_transient,
            "failed_permanent": self.failed_permanent,
        }


def _next_attempt(now: str, attempts: int) -> str:
    index = min(attempts, len(BACKOFF_SECONDS)) - 1
    delay = BACKOFF_SECONDS[max(index, 0)]
    moment = datetime.strptime(now, _TIME_FORMAT).replace(tzinfo=UTC)
    return (moment + timedelta(seconds=delay)).strftime(_TIME_FORMAT)


class EnrichmentService:
    """补全语义层：嵌入 → 分类 → 链接。

    不在写入路径上，因此模型不可用只会推迟补全，绝不影响正文落库。
    每一步都幂等，失败后整体重试的代价很低。
    """

    def __init__(
        self,
        *,
        repository: RecordRepository,
        store: VectorStore,
        embedder: Embedder,
        classifier: Classifier,
        generation: str,
        link_count: int = 3,
    ) -> None:
        self.repository = repository
        self.store = store
        self.embedder = embedder
        self.classifier = classifier
        self.generation = generation
        self.link_count = link_count

    def drain(self, *, limit: int | None = None, now: str | None = None) -> DrainResult:
        moment = now or datetime.now(UTC).strftime(_TIME_FORMAT)
        jobs = self.repository.due_jobs("enrich", now=moment, limit=limit or 20)
        succeeded = transient = permanent = 0
        for job in jobs:
            record = self.repository.get(job.record_id)
            text = f"{record.title}\n{record.body}"
            try:
                vector = self.embedder.embed([text])[0]
            except ModelUnavailableError as exc:
                # 暂时性：模型没起来，退避后重试仍有意义。
                attempts = job.attempts + 1
                self.repository.reschedule_job(
                    job.job_id,
                    attempts=attempts,
                    next_attempt_at=_next_attempt(moment, attempts),
                    error=str(exc),
                    failure_kind="transient",
                )
                transient += 1
                continue
            except ValueError as exc:
                # 确定性：同样的输入只会得到同样的坏输出，重试是浪费。
                self.repository.fail_job_permanently(job.job_id, error=str(exc))
                permanent += 1
                continue

            self.store.put(
                record.record_id,
                vector,
                model=self.embedder.model,
                dimensions=self.store.dimensions,
                generation=self.generation,
                input_hash=record.content_hash,
            )
            result = self.classifier.classify(record, vector)
            self._persist_facets(result.facets)
            neighbors = self.store.neighbors(
                vector, limit=self.link_count, exclude=record.record_id
            )
            build_links(self.repository.connection, record.record_id, neighbors)
            self.repository.complete_job(job.job_id)
            succeeded += 1
        return DrainResult(
            processed=len(jobs),
            succeeded=succeeded,
            failed_transient=transient,
            failed_permanent=permanent,
        )

    def _persist_facets(self, facets) -> None:
        with write_transaction(self.repository.connection) as tx:
            for facet in facets:
                tx.execute(
                    """
                    INSERT INTO facets(
                        record_id, kind, value, provenance, confidence, locked
                    ) VALUES (?, ?, ?, ?, ?, 0)
                    ON CONFLICT(record_id, kind, value) DO UPDATE SET
                        provenance = excluded.provenance,
                        confidence = excluded.confidence
                    WHERE facets.locked = 0
                    """,
                    (
                        facet.record_id,
                        facet.kind,
                        facet.value,
                        facet.provenance,
                        facet.confidence,
                    ),
                )
