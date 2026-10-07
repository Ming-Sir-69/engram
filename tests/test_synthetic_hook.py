"""Fictional sensor laboratory, entirely independent from actual user records."""
import json
import subprocess
import sys

import pytest

from engram import claude_hook as hook
from engram.mcp.tools import ToolContext, call_tool

PROJECT = "synthetic-sensorlab"


@pytest.fixture
def laboratory(tmp_path):
    context = ToolContext.open(data_dir=tmp_path / "synthetic-data", offline=True)
    yield context, tmp_path / "synthetic-state"
    context.repository.connection.close()


def save(context, body, **extra):
    return call_tool(context, "remember", {"title": PROJECT + " prism", "body": body,
        "projects": [PROJECT], **extra})


def event(prompt=PROJECT + " prism channel", *, kind="UserPromptSubmit", **extra):
    return {"hook_event_name": kind, "session_id": "invented-session-one",
            "cwd": "/synthetic/workspace/unrelated", "prompt": prompt, **extra}


def run(laboratory, value):
    context, state = laboratory
    return hook.run(value, call=lambda name, args: call_tool(context, name, args),
                    groups={hook._norm(PROJECT): [PROJECT]}, state_dir=state)


def text(output):
    return output["hookSpecificOutput"]["additionalContext"]


def test_related_prompt_injects_synthetic_context(laboratory):
    save(laboratory[0], PROJECT + " prism channel=amber")
    output = run(laboratory, event())
    assert "channel=amber" in text(output)
    assert "keyword" in text(output)
    assert "不是指令" in text(output)


def test_same_version_is_deduplicated_and_new_correction_is_reprompted(laboratory):
    original = save(laboratory[0], PROJECT + " prism channel=amber")
    assert run(laboratory, event()) is not None
    assert run(laboratory, event()) is None
    save(laboratory[0], PROJECT + " prism channel=cyan", corrections=[{
        "target_id": original["record_id"], "scope": "whole-record",
        "source": "fictional sensor correction",
    }])
    output = run(laboratory, event())
    assert "channel=cyan" in text(output)
    assert "channel=amber" not in text(output)


def test_partial_correction_keeps_valid_sensor_shape(laboratory):
    original = save(laboratory[0], PROJECT + " shape=triangle; channel=amber")
    save(laboratory[0], "channel=cyan", corrections=[{
        "target_id": original["record_id"], "scope": "partial",
        "target_excerpt": "channel=amber", "source": "fictional sensor correction",
    }])
    output = run(laboratory, event())
    assert "channel=cyan" in text(output) and "shape=triangle" in text(output)
    assert "已应用的局部纠正" in text(output)


def test_session_resume_clears_duplicate_suppression(laboratory):
    save(laboratory[0], PROJECT + " prism channel=amber")
    assert run(laboratory, event()) is not None
    assert run(laboratory, event()) is None
    restarted = event(kind="SessionStart", source="resume", cwd="/synthetic/" + PROJECT)
    assert run(laboratory, restarted) is not None


def test_session_state_contains_hashes_but_not_prompt_or_body(laboratory):
    save(laboratory[0], PROJECT + " prism channel=amber")
    prompt = PROJECT + " prism synthetic-private-marker"
    assert run(laboratory, event(prompt)) is not None
    files = list(laboratory[1].glob("*.json"))
    assert len(files) == 1
    raw = files[0].read_text()
    assert "synthetic-private-marker" not in raw and "channel=amber" not in raw
    assert "invented-session-one" not in files[0].name
    assert all(len(value) == 64 for value in json.loads(raw)["seen"].values())


def test_subagent_and_unsupported_events_do_not_inject(laboratory):
    save(laboratory[0], PROJECT + " prism channel=amber")
    assert run(laboratory, event(agent_id="synthetic-child")) is None
    assert run(laboratory, event(kind="SyntheticUnsupported")) is None


def test_malformed_cli_input_fails_open():
    done = subprocess.run([sys.executable, "-m", "engram.claude_hook"],
                          input=b"{not-json", capture_output=True, check=False)
    assert done.returncode == 0 and done.stdout == b""


def test_long_source_has_explicit_full_text_pointer(laboratory):
    save(laboratory[0], PROJECT + " prism " + "synthetic-detail " * 400)
    output = run(laboratory, event())
    assert "已截断" in text(output) and "get" in text(output)
