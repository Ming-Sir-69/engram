"""OAuth acceptance with isolated owner state and knowledge, no live credentials."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from engram.mcp.oauth import OwnerOAuth
from engram.mcp.remote import create_app

BASE = "https://engram.example"
ISSUER = BASE + "/engram"
CALLBACK = 'https://client.example.invalid/connector/oauth/test-connection'
TOKEN = "synthetic-static-token-long-enough-for-test"
VERIFIER = "a" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # The callback is invented; explicitly authorize only its synthetic host.
    monkeypatch.setenv("ENGRAM_MCP_OAUTH_REDIRECT_HOSTS", "client.example.invalid")
    for k in (
        "ENGRAM_EXPORT_DIR",
        "ENGRAM_INDEX_PATH",
        "ENGRAM_AUTONOMOUS_MAINTENANCE",
    ):
        monkeypatch.delenv(k, raising=False)
    app = create_app(
        data_dir=tmp_path / "knowledge",
        offline=True,
        token=TOKEN,
        public_url=BASE,
        public_path="/engram",
        oauth_state=tmp_path / "auth",
    )
    provider = OwnerOAuth(tmp_path / "auth", ISSUER)
    with TestClient(app, base_url=BASE) as client:
        yield client, provider


def start(client, resource=ISSUER + "/mcp"):
    r = client.post(
        "/register",
        json={
            "client_name": "ChatGPT test",
            "redirect_uris": [CALLBACK],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["client_id"]
    a = client.get(
        "/authorize",
        params={
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": CALLBACK,
            "code_challenge": CHALLENGE,
            "code_challenge_method": "S256",
            "state": "opaque-state",
            "scope": "engram:access",
            "resource": resource,
        },
        follow_redirects=False,
    )
    return cid, a


def approved_code(client, provider):
    cid, a = start(client)
    assert a.status_code == 302
    rid = a.headers["location"].rsplit("/", 1)[1]
    # Cookie path follows the public prefix, which the TLS proxy strips in prod.
    client.get("/consent/" + rid)
    cookie = next(iter(client.cookies.values()))
    client.headers["Cookie"] = (
        "engram_consent_" + hashlib.sha256(rid.encode()).hexdigest()[:16] + "=" + cookie
    )
    with pytest.raises(ValueError):
        provider.approve(rid, "https://evil.example/callback")
    provider.approve(rid, CALLBACK)
    a = client.get("/consent/" + rid, follow_redirects=False)
    assert a.status_code == 302
    q = parse_qs(urlsplit(a.headers["location"]).query)
    assert q["state"] == ["opaque-state"]
    assert client.get("/consent/" + rid).status_code == 400
    return cid, q["code"][0]


def test_owner_approval_pkce_replay_refresh_revoke(setup):
    c, p = setup
    cid, code = approved_code(c, p)
    data = {
        "grant_type": "authorization_code",
        "client_id": cid,
        "code": code,
        "redirect_uri": CALLBACK,
        "code_verifier": "b" * 64,
        "resource": p.resource,
    }
    assert c.post("/token", data=data).status_code == 400
    data["code_verifier"] = VERIFIER
    r = c.post("/token", data=data)
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert c.post("/token", data=data).status_code == 400
    headers = {
        "Authorization": "Bearer " + tokens["access_token"],
        "Accept": "application/json, text/event-stream",
    }
    r = c.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "status", "arguments": {}},
        },
    )
    assert r.status_code == 200 and not r.json()["result"]["isError"]
    refresh = {
        "grant_type": "refresh_token",
        "client_id": cid,
        "refresh_token": tokens["refresh_token"],
        "resource": p.resource,
    }
    newer = c.post("/token", data=refresh)
    assert newer.status_code == 200, newer.text
    assert c.post("/token", data=refresh).status_code == 400
    assert (
        c.post(
            "/revoke", data={"client_id": cid, "token": newer.json()["refresh_token"]}
        ).status_code
        == 200
    )
    assert c.get("/mcp", headers=headers).status_code == 401
    assert tokens["access_token"].encode() not in p.db.read_bytes()
    assert tokens["refresh_token"].encode() not in p.db.read_bytes()


def test_discovery_owner_and_audience_boundaries(setup):
    c, p = setup
    assert 'resource_metadata="' + ISSUER in c.get("/mcp").headers["www-authenticate"]
    assert (
        c.get("/.well-known/oauth-protected-resource").json()["resource"] == p.resource
    )
    assert c.get("/.well-known/oauth-authorization-server").json()[
        "code_challenge_methods_supported"
    ] == ["S256"]
    _, r = start(c, resource="https://evil.example/mcp")
    assert "error=" in r.headers["location"]
    _, r = start(c)
    rid = r.headers["location"].rsplit("/", 1)[1]
    with pytest.raises(ValueError):
        p.approve(rid, CALLBACK)
    assert c.get("/consent/" + rid).status_code == 200
    c.cookies.clear()
    assert c.get("/consent/" + rid).status_code == 403
    assert c.post("/approve", json={"request_id": rid}).status_code in (404, 405)
    assert (
        c.post(
            "/register",
            json={
                "redirect_uris": ["http://evil.example"],
                "grant_types": ["authorization_code", "refresh_token"],
            },
        ).status_code
        == 400
    )


def test_static_token_is_local_only_and_redirects_are_allowlisted(setup):
    c, p = setup
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    local = {
        "Authorization": "Bearer " + TOKEN,
        "Accept": "application/json, text/event-stream",
    }
    assert c.post("/mcp", headers=local, json=body).status_code == 200
    funnel = {**local, "Tailscale-Funnel-Request": "?1"}
    assert c.post("/mcp", headers=funnel, json=body).status_code == 401
    with p.connect() as db:
        oauth_token = p.issue(db, "test-client", ["engram:access"]).access_token
    via_oauth = {**funnel, "Authorization": "Bearer " + oauth_token}
    assert c.post("/mcp", headers=via_oauth, json=body).status_code == 200
    for uri in ("https://evil.example/cb", "https://chatgpt.com.evil.example/cb"):
        r = c.post(
            "/register",
            json={
                "redirect_uris": [uri],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert r.status_code == 400, uri


def test_refresh_replay_revokes_rotated_family_not_only_the_old_token(setup):
    c, p = setup
    cid, code = approved_code(c, p)
    original = c.post('/token', data={
        'grant_type': 'authorization_code', 'client_id': cid, 'code': code,
        'redirect_uri': CALLBACK, 'code_verifier': VERIFIER, 'resource': p.resource,
    }).json()
    old_request = {'grant_type': 'refresh_token', 'client_id': cid,
                   'refresh_token': original['refresh_token'], 'resource': p.resource}
    rotated_response = c.post('/token', data=old_request)
    assert rotated_response.status_code == 200
    rotated = rotated_response.json()
    assert c.post('/token', data=old_request).status_code == 400
    assert c.post('/token', data={**old_request, 'refresh_token': rotated['refresh_token']}).status_code == 400
    for access in (original['access_token'], rotated['access_token']):
        response = c.post('/mcp', headers={
            'Authorization': 'Bearer ' + access, 'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
        assert response.status_code == 401
    assert original['refresh_token'].encode() not in p.db.read_bytes()


def test_wrong_client_cannot_revoke_another_rotated_family(setup):
    c, p = setup
    cid, code = approved_code(c, p)
    original = c.post('/token', data={
        'grant_type': 'authorization_code', 'client_id': cid, 'code': code,
        'redirect_uri': CALLBACK, 'code_verifier': VERIFIER, 'resource': p.resource,
    }).json()
    old_request = {'grant_type': 'refresh_token', 'client_id': cid,
                   'refresh_token': original['refresh_token'], 'resource': p.resource}
    rotated = c.post('/token', data=old_request).json()
    other_cid, _ = start(c)
    assert c.post('/token', data={**old_request, 'client_id': other_cid}).status_code == 400
    assert c.post('/token', data={**old_request, 'refresh_token': rotated['refresh_token']}).status_code == 200


def test_spent_refresh_keeps_original_expiry_and_rejects_other_resource(setup):
    import asyncio

    from engram.mcp.oauth import digest

    c, p = setup
    cid, _ = start(c)
    client = asyncio.run(p.get_client(cid))
    with p.connect() as db:
        original = p.issue(db, cid, [p.scope])
    old = asyncio.run(p.load_refresh_token(client, original.refresh_token))
    rotated = asyncio.run(p.exchange_refresh_token(client, old, [p.scope]))
    with p.connect() as db:
        row = db.execute("SELECT value,expires FROM objects WHERE kind='spent_refresh' AND key=?",
                         (digest(original.refresh_token),)).fetchone()
    assert row is not None and row[1] == old.expires_at
    assert original.refresh_token not in row[0]
    other_resource = OwnerOAuth(p.directory, 'https://other.example.invalid/engram',
                                totp_file=p.directory / 'synthetic-absent-seed')
    assert asyncio.run(other_resource.load_refresh_token(client, original.refresh_token)) is None
    assert asyncio.run(other_resource.load_refresh_token(client, rotated.refresh_token)) is None
    assert asyncio.run(p.load_refresh_token(client, rotated.refresh_token)) is not None
