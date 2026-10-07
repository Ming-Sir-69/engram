"""Presentation profiles share isolated data and preserve transport boundaries."""

import io
import json
import sys
from pathlib import Path

import anyio
import pytest

from engram.cli import _build_parser, main
from engram.mcp.profile import CLAUDE_PROFILE, DEFAULT_PROFILE, get_profile
from engram.mcp.server import SERVER_VERSION, serve
from engram.mcp.tools import ToolContext, tool_descriptors


def exchange(context, *, profile="default", method="initialize", params=None):
    source = io.StringIO(
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        )
        + "\n"
    )
    sink = io.StringIO()
    serve(context, profile=profile, stdin=source, stdout=sink)
    return json.loads(sink.getvalue())["result"]


@pytest.fixture
def context(tmp_path):
    context = ToolContext.open(data_dir=tmp_path / "data", offline=True)
    try:
        yield context
    finally:
        context.repository.connection.close()


@pytest.mark.parametrize("profile", [DEFAULT_PROFILE, CLAUDE_PROFILE])
def test_stdio_profile_identity_and_tool_contract(context, profile):
    initialized = exchange(context, profile=profile.key)
    assert initialized["serverInfo"] == {
        "name": profile.server_name,
        "title": profile.title,
        "version": SERVER_VERSION,
    }
    assert initialized["instructions"] == profile.instructions
    listed = exchange(context, profile=profile.key, method="tools/list")
    assert listed["tools"] == tool_descriptors()
    assert context.client_profile == profile.key


def test_invalid_profile_fails_before_serving(context):
    with pytest.raises(ValueError, match="profile"):
        exchange(context, profile="other")
    with pytest.raises(ValueError, match="profile"):
        get_profile(None)


def test_cli_profile_defaults_and_stdio_forwarding(context, monkeypatch):
    assert _build_parser().parse_args(["mcp"]).profile == "default"
    source = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    sink = io.StringIO()
    monkeypatch.setattr(sys, "stdin", source)
    monkeypatch.setattr(sys, "stdout", sink)
    assert (
        main(
            [
                "--data-dir",
                str(context.config.data_dir),
                "mcp",
                "--offline",
                "--profile",
                "claude",
            ]
        )
        == 0
    )
    assert (
        json.loads(sink.getvalue())["result"]["serverInfo"]["name"] == "engram-claude"
    )


def test_cli_http_forwards_profile_without_changing_data_dir(tmp_path, monkeypatch):
    remote = pytest.importorskip("engram.mcp.remote")
    calls = []
    monkeypatch.setattr(remote, "run_remote", lambda **kwargs: calls.append(kwargs))
    assert (
        main(
            [
                "--data-dir",
                str(tmp_path),
                "mcp",
                "--transport",
                "streamable-http",
                "--profile",
                "claude",
                "--port",
                "18768",
                "--offline",
            ]
        )
        == 0
    )
    assert calls[0]["profile"] == "claude"
    assert calls[0]["data_dir"] == str(tmp_path)
    assert calls[0]["port"] == 18768
    assert calls[0]["offline"] is True


def test_offline_claude_stays_deterministic(context):
    from engram.embedding import DeterministicEmbedder

    exchange(context, profile="claude")
    assert isinstance(context._vector(), DeterministicEmbedder)


def test_online_claude_reuses_shared_embedding_without_local_fallback(
    context, monkeypatch
):
    from engram.embedding import MLXEmbedder, SharedMLXEmbedder

    context.offline = False
    exchange(context, profile="claude")
    embedder = context._vector()
    assert isinstance(embedder, SharedMLXEmbedder)
    assert embedder.base_url == "http://127.0.0.1:8772"
    assert embedder.allow_local_fallback is False

    def forbidden_local_model(*args, **kwargs):
        pytest.fail("Claude companion must not load a local embedding model")

    monkeypatch.setattr(MLXEmbedder, "embed", forbidden_local_model)
    from urllib.error import URLError

    def unavailable(*args, **kwargs):
        raise URLError(ConnectionRefusedError())

    monkeypatch.setattr("engram.embedding.urlopen", unavailable)
    from engram.errors import ModelUnavailableError

    with pytest.raises(ModelUnavailableError):
        embedder.embed_query("isolated profile acceptance")
    assert context._backfill(embedder)["deferred_to_shared_worker"] is True


def test_official_sdk_stdio_profile(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    source_root = Path(__file__).resolve().parents[1] / "src"

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-c",
                "from engram.cli import main; raise SystemExit(main())",
                "--data-dir",
                str(tmp_path / "data"),
                "mcp",
                "--offline",
                "--profile",
                "claude",
            ],
            env={"PYTHONPATH": str(source_root)},
        )
        with anyio.fail_after(10):
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    result = await session.initialize()
                    assert result.serverInfo.name == "engram-claude"
                    assert result.serverInfo.title == "Engram Claude"
                    assert result.serverInfo.version == "0.4.0"
                    listed = await session.list_tools()
                    assert {item.name for item in listed.tools} == {
                        item["name"] for item in tool_descriptors()
                    }
                    status = await session.call_tool("status", {})
                    assert not status.isError
                    assert json.loads(status.content[0].text)["records"] == 0

    anyio.run(scenario)
