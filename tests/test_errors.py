import json

from engram.errors import (
    DatabaseBusyError,
    EngramError,
    InvalidInputError,
    ModelUnavailableError,
    RecordNotFoundError,
    exit_code_for,
)


def test_problem_detail_always_has_required_fields() -> None:
    problem = InvalidInputError("record_type must be one of note/reference/project")
    payload = problem.to_problem(instance="cli:record-create").to_dict()
    for field in (
        "type",
        "title",
        "status",
        "detail",
        "instance",
        "code",
        "retryable",
        "context",
    ):
        assert field in payload
    assert json.dumps(payload)


def test_database_busy_is_retryable_with_delay() -> None:
    error = DatabaseBusyError(owner_pid=4321, retry_after_seconds=5)
    problem = error.to_problem(instance="cli:record-create")
    assert problem.code == "SB-423-DATABASE-BUSY"
    assert problem.retryable is True
    assert problem.retry_after_seconds == 5
    assert problem.context["owner_pid"] == 4321
    assert exit_code_for(error) == 75


def test_missing_record_maps_to_66() -> None:
    assert exit_code_for(RecordNotFoundError("rec_missing")) == 66


def test_invalid_input_maps_to_65() -> None:
    assert exit_code_for(InvalidInputError("bad")) == 65


def test_model_unavailable_maps_to_69_and_is_retryable() -> None:
    error = ModelUnavailableError("ollama unreachable")
    assert exit_code_for(error) == 69
    assert error.retryable is True


def test_internal_details_never_leak_into_public_payload() -> None:
    try:
        raise ValueError("secret path /Users/private/data")
    except ValueError as exc:
        error = EngramError("internal failure", cause=exc)
    payload = error.to_problem(instance="cli:x").to_dict()
    assert "secret path" not in json.dumps(payload)
    assert "Traceback" not in json.dumps(payload)
