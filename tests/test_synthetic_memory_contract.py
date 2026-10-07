"""Invented sensor fixtures; no actual memory or event has been transformed here."""
import pytest

from engram.db import write_transaction
from engram.domain import content_hash_for
from engram.errors import InvalidInputError, ModelUnavailableError
from engram.mcp.tools import ToolContext, call_tool


@pytest.fixture
def library(tmp_path):
    context = ToolContext.open(data_dir=tmp_path / "synthetic-library", offline=True)
    yield context
    context.repository.connection.close()


def put(context, title, body, **extra):
    return call_tool(context, "remember", {"title": title, "body": body, **extra})


def correct(context, target, body, scope="whole-record", **extra):
    return put(context, "Synthetic sensor correction", body, corrections=[{
        "target_id": target["record_id"], "scope": scope,
        "source": "invented sensor fixture", **extra,
    }])


def get(context, record):
    return call_tool(context, "get", {"record_id": record["record_id"]})


def revise(context, record, body):
    with write_transaction(context.repository.connection) as tx:
        tx.execute("UPDATE records SET body=?,content_hash=?,revision=revision+1 WHERE record_id=?",
                   (body, content_hash_for(record["title"], body), record["record_id"]))


def test_commit_is_readable_before_vector_enrichment(library):
    record = put(library, "Synthetic prism", "synthetic-prism channel amber")
    ready = get(library, record)["readiness"]
    assert ready["committed"] and ready["id_ready"] and ready["keyword_ready"]
    assert not ready["vector_ready"]
    result = call_tool(library, "recall", {"query": "synthetic-prism"})
    assert result["results"][0]["record_id"] == record["record_id"]


def test_whole_correction_resolves_old_words_and_preserves_history(library):
    original = put(library, "Synthetic prism", "obsolete-amber channel")
    new = correct(library, original, "current-cyan channel")
    result = call_tool(library, "recall", {"query": "obsolete-amber"})
    assert result["results"][0]["record_id"] == new["record_id"]
    assert get(library, original)["body"] == "obsolete-amber channel"
    assert get(library, original)["current"]["status"] == "superseded"
    history = call_tool(library, "recall", {"query": "obsolete-amber", "view": "history"})
    assert history["results"][0]["record_id"] == original["record_id"]


def test_exact_partial_correction_retains_unaffected_sensor_field(library):
    original = put(library, "Synthetic prism", "shape=triangle; channel=amber; size=7")
    new = correct(library, original, "channel=cyan", "partial", target_excerpt="channel=amber",
                  baseline_hash=original["content_hash"])
    view = get(library, original)["current"]["current_records"][0]
    assert view["current_text"] == "shape=triangle; channel=cyan; size=7"
    assert view["body"] == original["body"]
    assert any(item["record_id"] == new["record_id"] for item in view["correction_chain"])


def test_changed_baseline_does_not_retarget_equal_text(library):
    original = put(library, "Synthetic prism", "first=amber; second=cyan")
    correct(library, original, "first=violet", "partial", target_excerpt="first=amber")
    revise(library, original, "first=amber; second=gold; new-field=true")
    view = get(library, original)["current"]["current_records"][0]
    assert view["partial_corrections"][0]["baseline_changed"]
    assert not view["partial_corrections"][0]["applied_exactly"]
    assert "first=amber; second=gold" in view["current_text"]


def test_overlap_keeps_sources_and_declares_unresolved_scope(library):
    original = put(library, "Synthetic overlap", "reading=12345; keep=triangle")
    correct(library, original, "reading=888", "partial", target_excerpt="reading=123")
    correct(library, original, "value=999", "partial", target_excerpt="12345")
    view = get(library, original)["current"]["current_records"][0]
    assert all(note["overlapping_scope"] for note in view["partial_corrections"])
    assert all(not note["applied_exactly"] for note in view["partial_corrections"])
    assert "reading=12345; keep=triangle" in view["current_text"]


def test_disjoint_scopes_use_original_coordinates(library):
    original = put(library, "Synthetic prism", "channel=amber; size=7")
    correct(library, original, "channel=cyan", "partial", target_excerpt="channel=amber")
    correct(library, original, "size=9", "partial", target_excerpt="size=7")
    assert get(library, original)["current"]["current_records"][0]["current_text"] == "channel=cyan; size=9"


def test_baseline_failure_rolls_back_record_fts_and_outbox(library):
    original = put(library, "Synthetic prism", "channel=amber")
    before = library.repository.count()
    with pytest.raises(InvalidInputError):
        correct(library, original, "channel=cyan", baseline_hash="synthetic-wrong-hash")
    assert library.repository.count() == before
    assert not call_tool(library, "recall", {"query": "cyan"})["results"]


def test_missing_project_metadata_does_not_block_keyword_recall(library):
    original = put(library, "Synthetic prism", "synthetic-unregistered reading=7")
    result = call_tool(library, "recall", {"query": "synthetic-unregistered", "projects": ["synthetic-unused"]})
    assert result["results"][0]["record_id"] == original["record_id"]


def test_duplicate_body_can_add_new_correction_binding(library):
    original = put(library, "Synthetic old", "channel=amber")
    replacement = put(library, "Synthetic new", "channel=cyan")
    rebound = put(library, replacement["title"], replacement["body"], projects=["synthetic-prism"],
                  corrections=[{"target_id": original["record_id"], "scope": "whole-record", "source": "synthetic rebinding"}])
    assert rebound["record_id"] == replacement["record_id"]
    assert library.repository.count() == 2
    assert get(library, original)["current"]["status"] == "superseded"
    assert "synthetic-prism" in get(library, replacement)["projects"]


def test_semantic_failure_returns_explicit_keyword_degradation(library, monkeypatch):
    original = put(library, "Synthetic prism", "synthetic-fallback channel=amber")
    def unavailable(self):
        raise ModelUnavailableError("invented model failure")
    monkeypatch.setattr(ToolContext, "_vector", unavailable)
    result = call_tool(library, "recall", {"query": "synthetic-fallback", "mode": "hybrid"})
    assert result["mode"] == "keyword" and result["requested_mode"] == "hybrid"
    assert result["degraded"] and result["semantic_error_type"] == "ModelUnavailableError"
    assert result["results"][0]["record_id"] == original["record_id"]


def test_deterministic_vector_backfill_is_not_a_readability_requirement(library):
    original = put(library, "Synthetic prism", "synthetic-vector-prism channel=amber")
    result = call_tool(library, "recall", {"query": "synthetic-vector-prism", "mode": "vector"})
    assert result["backfilled"]["succeeded"] == 1
    assert any(item["record_id"] == original["record_id"] for item in result["results"])
    assert get(library, original)["readiness"]["vector_ready"]


def test_separate_connection_observes_committed_correction(library):
    original = put(library, "Synthetic prism", "synthetic-cross-connection old-channel")
    reader = ToolContext.open(data_dir=library.config.data_dir, offline=True)
    try:
        replacement = correct(library, original, "synthetic-cross-connection new-channel")
        result = call_tool(reader, "recall", {"query": "old-channel"})
        assert result["results"][0]["record_id"] == replacement["record_id"]
    finally:
        reader.repository.connection.close()


def test_correction_cycle_is_rejected_without_partial_commit(library):
    first = put(library, "Synthetic first", "first sensor")
    second = correct(library, first, "second sensor")
    with pytest.raises(InvalidInputError):
        put(library, first["title"], first["body"], corrections=[{
            "target_id": second["record_id"], "scope": "whole-record", "source": "synthetic cycle",
        }])
    assert library.repository.count() == 2
    assert get(library, first)["current"]["status"] == "superseded"
