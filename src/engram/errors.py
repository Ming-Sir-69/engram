from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

_BASE_TYPE = "https://engram.local/problems"


@dataclass(frozen=True, slots=True)
class ProblemDetail:
    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str
    retryable: bool
    retry_after_seconds: int | None
    context: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.type,
            "title": self.title,
            "status": self.status,
            "detail": self.detail,
            "instance": self.instance,
            "code": self.code,
            "retryable": self.retryable,
            "context": dict(self.context),
        }
        if self.retry_after_seconds is not None:
            payload["retry_after_seconds"] = self.retry_after_seconds
        return payload


class EngramError(Exception):
    code = "SB-500-INTERNAL"
    status = 500
    title = "Internal error"
    exit_code = 70
    retryable = False

    def __init__(
        self,
        detail: str,
        *,
        context: Mapping[str, object] | None = None,
        retry_after_seconds: int | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.context = dict(context or {})
        self.retry_after_seconds = retry_after_seconds
        self._cause = cause

    def to_problem(self, *, instance: str) -> ProblemDetail:
        return ProblemDetail(
            type=f"{_BASE_TYPE}/{self.code.lower()}",
            title=self.title,
            status=self.status,
            detail=self.detail,
            instance=instance,
            code=self.code,
            retryable=self.retryable,
            retry_after_seconds=self.retry_after_seconds,
            context=self.context,
        )


class InvalidInputError(EngramError):
    code = "SB-400-INVALID-INPUT"
    status = 400
    title = "Invalid input"
    exit_code = 65


class RecordNotFoundError(EngramError):
    code = "SB-404-RECORD-NOT-FOUND"
    status = 404
    title = "Record not found"
    exit_code = 66

    def __init__(self, record_id: str) -> None:
        super().__init__(
            f"record {record_id} does not exist", context={"record_id": record_id}
        )


class DatabaseBusyError(EngramError):
    code = "SB-423-DATABASE-BUSY"
    status = 423
    title = "Database is busy"
    exit_code = 75
    retryable = True

    def __init__(
        self, *, owner_pid: int | None = None, retry_after_seconds: int = 5
    ) -> None:
        super().__init__(
            "another writer holds the database lock",
            context={"owner_pid": owner_pid},
            retry_after_seconds=retry_after_seconds,
        )


class ModelUnavailableError(EngramError):
    code = "SB-503-MODEL-UNAVAILABLE"
    status = 503
    title = "Local model unavailable"
    exit_code = 69
    retryable = True


class SchemaIncompatibleError(EngramError):
    code = "SB-409-SCHEMA-INCOMPATIBLE"
    status = 409
    title = "Schema version incompatible"
    exit_code = 78


def exit_code_for(error: EngramError) -> int:
    return error.exit_code
