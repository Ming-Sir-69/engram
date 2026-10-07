"""Remote transport uses isolated data; never touches the personal knowledge base."""

import json

import anyio
import pytest

pytest.importorskip("mcp")
from starlette.testclient import TestClient

from engram.mcp.remote import AdmissionControl, create_app

TOKEN = "test-only-not-a-production-key-12345"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGRAM_EXPORT_DIR", raising=False)
    monkeypatch.delenv("ENGRAM_INDEX_PATH", raising=False)
    with TestClient(
        create_app(data_dir=tmp_path, offline=True, token=TOKEN),
        base_url="http://127.0.0.1",
    ) as client:
        yield client


def rpc(client, method, params=None):
    response = client.post(
        "/mcp",
        headers=HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_remote_four_tools_roundtrip_and_errors(client):
    result = rpc(
        client,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "acceptance", "version": "1"},
        },
    )
    assert result["protocolVersion"] == "2025-06-18"
    listed = rpc(client, "tools/list")["tools"]
    assert {t["name"] for t in listed} == {
        "feedback",
        "remember",
        "recall",
        "get",
        "status",
        "inspect_record",
        "engram_maintenance_status",
        "engram_maintenance_catalog",
        "engram_maintenance_read",
    }
    for t in listed:
        assert t["annotations"]["readOnlyHint"] == (t["name"] in {"get", "status", "inspect_record"} or t["name"].startswith("engram_maintenance_"))

    def call(name, arguments):
        result = rpc(client, "tools/call", {"name": name, "arguments": arguments})
        assert not result["isError"], result
        return json.loads(result["content"][0]["text"])

    saved = call("remember", {"title": "隔离验收", "body": "quartz sentinel 739"})
    recalled = call("recall", {"query": "quartz", "mode": "keyword"})
    assert recalled["results"][0]["record_id"] == saved["record_id"]
    assert (
        call("get", {"record_id": saved["record_id"]})["body"] == "quartz sentinel 739"
    )
    assert call("status", {})["records"] == 1
    inspected = call("inspect_record", {"record_id": saved["record_id"]})
    assert inspected["record"]["body"] == "quartz sentinel 739"
    assert inspected["content_hash"] == saved["content_hash"]
    assert "facets" in inspected and "revisions" in inspected
    feedback = call(
        "feedback",
        {"action": "add", "summary": "remote inbox smoke test", "category": "ux"},
    )
    assert feedback["status"] == "open"
    assert call("status", {"detail": "feedback"})["feedback"]["open"] == 1
    assert rpc(client, "tools/call", {"name": "remember", "arguments": {}})["isError"]


@pytest.mark.parametrize("path", ["/mcp", "/sse", "/messages/"])
def test_auth_covers_every_mcp_endpoint(client, path):
    assert client.get(path).status_code == 401
    assert (
        client.post(path, headers={"Authorization": "Bearer wrong"}).status_code == 401
    )


def test_origin_host_and_body_limits(client):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    for path in ["/mcp", "/messages/"]:
        response = client.post(
            path, json=payload, headers={**HEADERS, "Origin": "https://evil.invalid"}
        )
        assert response.status_code == 403
    response = client.get("/sse", headers={**HEADERS, "Origin": "https://evil.invalid"})
    assert response.status_code == 403
    response = client.post(
        "/mcp", json=payload, headers={**HEADERS, "Host": "evil.invalid"}
    )
    assert response.status_code == 421
    response = client.post(
        "/mcp",
        content=b"x" * (4 * 1024 * 1024 + 1),
        headers={**HEADERS, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_service_fails_closed():
    with pytest.raises(ValueError):
        create_app()
    with pytest.raises(ValueError):
        create_app(host="0.0.0.0", loopback_no_auth=True)
    with pytest.raises(ValueError):
        create_app(token="test-short")
    with pytest.raises(ValueError):
        create_app(token=TOKEN, public_url="http://engram.example.com")
    with pytest.raises(ValueError):
        create_app(loopback_no_auth=True, public_url="https://engram.example.com")


@pytest.mark.parametrize("path", ["/mcp", "/sse", "/messages/", "/messages/synthetic-session"])
@pytest.mark.parametrize("authorization", [None, "Bearer synthetic-wrong-token"])
def test_prefixed_asgi_paths_require_bearer(tmp_path, path, authorization):
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    from engram.mcp.remote import BearerAuth

    async def synthetic_endpoint(request):
        return JSONResponse({"synthetic_downstream_reached": True})

    app = BearerAuth(Starlette(routes=[Route("/{remaining:path}", synthetic_endpoint)]), TOKEN)
    with TestClient(app, base_url="https://synthetic.example", root_path="/prefix") as c:
        headers = {"Accept": "application/json, text/event-stream"}
        if authorization is not None:
            headers["Authorization"] = authorization
        response = c.get("/prefix" + path, headers=headers)
        assert response.status_code == 401


def test_prefixed_auth_preserves_valid_tokens_and_funnel_boundary(tmp_path, monkeypatch):
    import asyncio

    from engram.mcp.oauth import OwnerOAuth

    monkeypatch.setenv("OWNER_TOTP_FILE", str(tmp_path / "synthetic-absent-seed"))
    app = create_app(data_dir=tmp_path / "synthetic-knowledge", offline=True,
        token=TOKEN, public_url="https://synthetic.example", public_path="/prefix",
        oauth_state=tmp_path / "synthetic-oauth")
    provider = OwnerOAuth(tmp_path / "synthetic-oauth", "https://synthetic.example/prefix",
                          totp_file=tmp_path / "synthetic-absent-seed")
    with provider.connect() as db:
        access = provider.issue(db, "synthetic-client", ["engram:access"]).access_token
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with TestClient(app, base_url="https://synthetic.example", root_path="/prefix") as c:
        assert c.post("/mcp", json=payload).status_code == 401
        assert c.post("/prefix/mcp", json=payload).status_code == 401
        assert c.post("/prefix/mcp", headers={**HEADERS, "Authorization": "Bearer synthetic-wrong"}, json=payload).status_code == 401
        assert c.post("/prefix/mcp", headers=HEADERS, json=payload).status_code == 200
        public = {**HEADERS, "Tailscale-Funnel-Request": "?1"}
        assert c.post("/prefix/mcp", headers=public, json=payload).status_code == 401
        public["Authorization"] = "Bearer " + access
        assert c.post("/prefix/mcp", headers=public, json=payload).status_code == 200
        asyncio.run(provider.revoke_token(asyncio.run(provider.load_access_token(access))))
        assert c.post("/prefix/mcp", headers=public, json=payload).status_code == 401


def test_initialize_declares_svg_icon_mime(tmp_path):
    app = create_app(data_dir=tmp_path / "synthetic-knowledge", offline=True,
        token=TOKEN, public_url="https://synthetic.example", public_path="/prefix")
    with TestClient(app, base_url="https://synthetic.example") as c:
        result = rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "synthetic-client", "version": "1"}})
        assert result["serverInfo"]["icons"] == [
            {"src": "https://synthetic.example/prefix/icon.svg", "mimeType": "image/svg+xml"}]


def test_public_health_and_icon_do_not_expose_knowledge(client):
    assert client.get("/healthz").json() == {
        "service": "engram",
        "transport": "http",
        "ok": True,
    }
    icon = client.get("/icon.svg")
    assert icon.status_code == 200
    assert icon.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in icon.content


@pytest.mark.parametrize("root_path", ["", "/prefix"])
def test_admission_control_returns_retryable_429_when_queue_is_full(root_path):
    async def scenario():
        release = anyio.Event()
        responses = []

        async def downstream(scope, receive, send):
            await release.wait()
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        controller = AdmissionControl(
            downstream, max_in_flight=1, queue_limit=0, request_timeout=2
        )
        scope = {
            "type": "http",
            "path": root_path + "/mcp",
            "root_path": root_path,
            "method": "POST",
            "headers": [],
            "scheme": "http",
            "http_version": "1.1",
            "query_string": b"",
            "client": ("test", 1),
            "server": ("test", 80),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            responses.append(message)

        async with anyio.create_task_group() as group:
            group.start_soon(controller, scope, receive, send)
            await anyio.sleep(0.02)
            second = []

            async def send_second(message):
                second.append(message)

            await controller(scope, receive, send_second)
            assert second[0]["status"] == 429
            assert dict(second[0]["headers"])[b"retry-after"] == b"1"
            release.set()

        assert responses[0]["status"] == 200

    anyio.run(scenario)


@pytest.mark.parametrize("root_path", ["", "/prefix"])
def test_timeout_only_admission_has_no_missing_limiter_release(root_path):
    async def scenario():
        sent = []

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"synthetic"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        controller = AdmissionControl(downstream, max_in_flight=None,
                                      queue_limit=None, request_timeout=1)
        await controller({"type": "http", "path": root_path + "/mcp", "root_path": root_path},
                         receive, send)
        assert sent[0]["status"] == 200

    anyio.run(scenario)
