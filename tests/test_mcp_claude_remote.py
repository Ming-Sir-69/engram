"""Claude companion uses the same SDK and OAuth boundaries as the default MCP."""

import json
import socket

import anyio
import pytest

pytest.importorskip("mcp")
from starlette.testclient import TestClient

from engram.mcp import remote
from engram.mcp.profile import CLAUDE_PROFILE
from engram.mcp.tools import tool_descriptors

TOKEN = "synthetic-claude-profile-only-123456"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json, text/event-stream",
}


def rpc(client, method, params=None):
    response = client.post(
        "/mcp",
        headers=HEADERS,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def call(client, name, arguments):
    result = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    assert not result["isError"], result
    return json.loads(result["content"][0]["text"])


def test_http_profile_identity_and_shared_data(tmp_path):
    for profile in ("claude", "default"):
        with TestClient(
            remote.create_app(
                data_dir=tmp_path,
                offline=True,
                profile=profile,
                token=TOKEN,
            ),
            base_url="http://127.0.0.1",
        ) as client:
            initialized = rpc(
                client,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "isolated-sdk-test", "version": "1"},
                },
            )
            expected_name = "engram-claude" if profile == "claude" else "engram"
            assert initialized["serverInfo"]["name"] == expected_name
            assert initialized["serverInfo"]["version"] == "0.4.0"
            if profile == "claude":
                assert initialized["instructions"] == CLAUDE_PROFILE.instructions
                saved = call(client, "remember", {"body": "isolated shared profile"})
            else:
                assert (
                    call(client, "get", {"record_id": saved["record_id"]})["body"]
                    == "isolated shared profile"
                )
            assert {tool["name"] for tool in rpc(client, "tools/list")["tools"]} == {
                tool["name"] for tool in tool_descriptors()
            }
            assert call(client, "status", {})["records"] == 1
            assert client.get("/healthz").json() == {
                "service": expected_name,
                "transport": "http",
                "ok": True,
            }


def test_http_tool_context_uses_claude_profile(tmp_path, monkeypatch):
    captured = []

    def observe(context, params):
        captured.append(context.client_profile)
        return {"content": [{"type": "text", "text": "{}"}], "isError": False}

    monkeypatch.setattr(remote, "_call", observe)
    with TestClient(
        remote.create_app(
            data_dir=tmp_path,
            offline=True,
            profile="claude",
            token=TOKEN,
        ),
        base_url="http://127.0.0.1",
    ) as client:
        rpc(client, "tools/call", {"name": "status", "arguments": {}})
    assert captured == ["claude"]


def test_official_sdk_http_profile(tmp_path):
    import httpx
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def scenario():
        app = remote.create_app(
            data_dir=tmp_path,
            offline=True,
            profile="claude",
            token=TOKEN,
        )
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical"))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            port = listener.getsockname()[1]

            async def run_server():
                await server.serve(sockets=[listener])

            with anyio.fail_after(10):
                async with anyio.create_task_group() as group:
                    group.start_soon(run_server)
                    try:
                        while not server.started:
                            await anyio.sleep(0.01)
                        async with (
                            httpx.AsyncClient(
                                headers=HEADERS,
                                trust_env=False,
                            ) as http_client,
                            streamable_http_client(
                                f"http://127.0.0.1:{port}/mcp",
                                http_client=http_client,
                            ) as (reader, writer, _),
                            ClientSession(reader, writer) as session,
                        ):
                            result = await session.initialize()
                            assert result.serverInfo.name == "engram-claude"
                            assert result.instructions == CLAUDE_PROFILE.instructions
                            listed = await session.list_tools()
                            assert {item.name for item in listed.tools} == {
                                item["name"] for item in tool_descriptors()
                            }
                            status = await session.call_tool("status", {})
                            assert not status.isError
                            assert json.loads(status.content[0].text)["records"] == 0
                    finally:
                        server.should_exit = True

    anyio.run(scenario)


def test_claude_auth_host_origin_and_funnel_boundaries(tmp_path):
    with TestClient(
        remote.create_app(
            data_dir=tmp_path,
            offline=True,
            profile="claude",
            token=TOKEN,
        ),
        base_url="http://127.0.0.1",
    ) as client:
        for path in ("/mcp", "/sse", "/messages/"):
            assert client.post(path).status_code == 401
            assert (
                client.post(
                    path,
                    headers={
                        **HEADERS,
                        "Tailscale-Funnel-Request": "1",
                    },
                ).status_code
                == 401
            )
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert (
            client.post(
                "/mcp",
                json=payload,
                headers={
                    **HEADERS,
                    "Origin": "https://untrusted.invalid",
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/mcp",
                json=payload,
                headers={
                    **HEADERS,
                    "Host": "untrusted.invalid",
                },
            ).status_code
            == 421
        )
    with pytest.raises(ValueError):
        remote.create_app(profile="claude")
    with pytest.raises(ValueError):
        remote.create_app(profile="claude", loopback_no_auth=True, host="0.0.0.0")


@pytest.mark.parametrize(
    "profile,title,path",
    [
        ("default", "Engram", "/engram"),
        ("claude", "Engram Claude", "/engram-claude"),
    ],
)
def test_oauth_resource_title_keeps_issuer_and_scope(tmp_path, profile, title, path):
    with TestClient(
        remote.create_app(
            data_dir=tmp_path / "data",
            offline=True,
            profile=profile,
            token=TOKEN,
            public_url="https://profiles.example.invalid",
            public_path=path,
            oauth_state=tmp_path / "oauth",
        ),
        base_url="https://profiles.example.invalid",
    ) as client:
        metadata = client.get("/.well-known/oauth-protected-resource").json()
        assert metadata == {
            "resource": 'https://profiles.example.invalid' + path + '/mcp',
            "authorization_servers": ['https://profiles.example.invalid' + path],
            "scopes_supported": ["engram:access"],
            "resource_name": title,
        }


@pytest.mark.parametrize("profile,offline", [("claude", False), ("default", True)])
def test_companion_and_offline_do_not_start_embedding_listener(
    tmp_path, monkeypatch, profile, offline
):
    monkeypatch.setenv("ENGRAM_MCP_TOKEN", TOKEN)
    for key in (
        "ENGRAM_MCP_TOKEN_FILE",
        "ENGRAM_MCP_OAUTH_STATE",
        "ENGRAM_MCP_PUBLIC_PATH",
        "ENGRAM_MCP_MAX_IN_FLIGHT",
        "ENGRAM_MCP_QUEUE_LIMIT",
        "ENGRAM_MCP_REQUEST_TIMEOUT",
        "ENGRAM_MCP_OAUTH_RATE_LIMIT",
        "ENGRAM_MCP_OAUTH_BODY_LIMIT",
        "ENGRAM_MCP_TOKEN_OVER_FUNNEL",
    ):
        monkeypatch.delenv(key, raising=False)
    calls = []
    monkeypatch.setattr(
        remote.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs)
    )

    def forbidden_listener(*args, **kwargs):
        pytest.fail("companion/offline must not claim shared model port 8772")

    monkeypatch.setattr(
        "engram.embedding_server.start_embedding_server",
        forbidden_listener,
    )
    remote.run_remote(data_dir=tmp_path, profile=profile, offline=offline, port=18768)
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 18768
