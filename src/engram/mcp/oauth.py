"""Owner-approved OAuth using the official MCP SDK's protocol handlers.

Only a local operator can approve a browser-bound request. The public endpoints
cannot approve themselves. Opaque tokens are stored by hash outside the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

try:
    from .owner_totp import (
        TOTP_LABEL,
        TOTP_MAX_FAILS,
        check_totp,
        read_secret,
        totp_now,
    )
except ImportError:  # Retain the existing standalone local approval CLI.
    from owner_totp import (
        TOTP_LABEL,
        TOTP_MAX_FAILS,  # noqa: F401 - compatibility re-export
        check_totp,
        read_secret,
        totp_now,  # noqa: F401 - compatibility re-export
    )

logger = logging.getLogger("uvicorn.error")

SCOPE = "engram:access"
DEFAULT_STATE = Path.home() / ".local/share/engram-mcp/oauth"
# HTTPS callbacks are limited to the hosted clients the owner actually links.
DEFAULT_REDIRECT_HOSTS = ("chatgpt.com", "claude.ai", "claude.com")


# tailscaled stamps every Funnel-relayed request with this header and overwrites any
# client-supplied value, so public callers cannot strip it (verified 2026-09-29, 1.102).
# Services sharing this module treat such requests as public: OAuth only, no static tokens.
FUNNEL_HEADER = b"tailscale-funnel-request"


def is_funnel_request(headers) -> bool:
    """True for requests relayed by Tailscale Funnel; ``headers`` is an ASGI header list or dict."""
    items = headers.items() if isinstance(headers, dict) else headers
    return any(name.lower() == FUNNEL_HEADER for name, _ in items)


def redirect_hosts_from_env():
    raw = os.environ.get("ENGRAM_MCP_OAUTH_REDIRECT_HOSTS", "")
    hosts = tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return hosts or DEFAULT_REDIRECT_HOSTS


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerOAuth:
    def __init__(
        self,
        directory,
        issuer,
        *,
        scope=SCOPE,
        resource_name="Engram Personal",
        redirect_hosts=None,
        totp_file=None,
    ):
        self.totp_file = totp_file if totp_file is not None else os.environ.get("OWNER_TOTP_FILE", str(Path.home() / ".local/share/mcp-owner-auth/owner-totp"))
        self.cookie_prefix = 'engram'
        self.scope_desc = "本人使用本服务全部已公开工具"
        self.redirect_hosts = tuple(redirect_hosts or redirect_hosts_from_env())
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.db = self.directory / "oauth.sqlite3"
        self.issuer = issuer.rstrip("/")
        self.resource = self.issuer + "/mcp"
        self.scope = scope
        self.resource_name = resource_name
        self.consent_path = urlsplit(self.issuer).path.rstrip("/") + "/consent/"
        with self.connect() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS objects (kind TEXT, key TEXT, value TEXT, expires REAL, PRIMARY KEY(kind,key))"
            )
        self.db.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=5)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def put(self, c, kind, key, value, expires):
        c.execute(
            "INSERT OR REPLACE INTO objects VALUES(?,?,?,?)",
            (kind, digest(key), json.dumps(value), expires),
        )

    def get(self, kind, key, *, c=None):
        if c is None:
            with self.connect() as connection:
                return self.get(kind, key, c=connection)
        row = c.execute(
            "SELECT value FROM objects WHERE kind=? AND key=? AND expires>?",
            (kind, digest(key), time.time()),
        ).fetchone()
        return json.loads(row[0]) if row else None

    async def get_client(self, client_id):
        v = self.get("client", client_id)
        return OAuthClientInformationFull.model_validate(v) if v else None

    async def register_client(self, client_info):
        for uri in client_info.redirect_uris:
            u = urlsplit(str(uri))
            if (
                u.username
                or u.password
                or u.fragment
                or not u.hostname
                or not (
                    (
                        u.scheme == "https"
                        and any(
                            u.hostname == h or u.hostname.endswith("." + h)
                            for h in self.redirect_hosts
                        )
                    )
                    or (
                        u.scheme == "http"
                        and u.hostname in {"127.0.0.1", "localhost", "::1"}
                    )
                )
            ):
                raise RegistrationError(
                    "invalid_redirect_uri",
                    "Redirect must be loopback or an allowed HTTPS client host",
                )
        if client_info.token_endpoint_auth_method not in {
            "none",
            "client_secret_post",
            "client_secret_basic",
        }:
            raise RegistrationError(
                "invalid_client_metadata", "Unsupported client authentication"
            )
        with self.connect() as c:
            c.execute("DELETE FROM objects WHERE expires<=?", (time.time(),))
            if (
                c.execute(
                    "SELECT count(*) FROM objects WHERE kind='client'"
                ).fetchone()[0]
                >= 256
            ):
                raise RegistrationError(
                    "invalid_client_metadata", "Registration capacity reached"
                )
            self.put(
                c,
                "client",
                client_info.client_id,
                client_info.model_dump(mode="json"),
                32503680000,
            )

    async def authorize(self, client, params):
        if params.resource not in {None, self.resource}:
            raise AuthorizeError("invalid_request", "Wrong resource")
        if not set(params.scopes or [self.scope]).issubset({self.scope}):
            raise AuthorizeError("invalid_scope", "Unsupported scope")
        rid = secrets.token_urlsafe(24)
        with self.connect() as c:
            c.execute("DELETE FROM objects WHERE expires<=?", (time.time(),))
            if (
                c.execute(
                    "SELECT count(*) FROM objects WHERE kind='pending'"
                ).fetchone()[0]
                >= 128
            ):
                raise AuthorizeError(
                    "temporarily_unavailable", "Pending capacity reached"
                )
            self.put(
                c,
                "pending",
                rid,
                {
                    "request_id": rid,
                    "client_id": client.client_id,
                    "client_name": client.client_name,
                    "params": params.model_dump(mode="json"),
                    "binding": None,
                    "approved": False,
                },
                time.time() + 600,
            )
        return self.issuer + "/consent/" + rid

    def _pending_record(self, c, rid):
        row = c.execute(
            "SELECT value,expires FROM objects WHERE kind='pending' AND key=?",
            (digest(rid),),
        ).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None, None)

    def _event(self, phase, http_status=None, *, rid=None, request_hash=None):
        # Never log request bodies, redirects, codes, verifier material or tokens.
        logger.info("owner_oauth %s", json.dumps({
            "gate": self.cookie_prefix,
            "request_hash": digest(rid)[:16] if rid is not None else request_hash,
            "phase": phase,
            "http_status": http_status,
        }, separators=(",", ":")))

    def _cookie_name(self, rid):
        return self.cookie_prefix + "_consent_" + digest(rid)[:16]

    def approve(self, rid, expected_redirect):
        # Deliberately local-only: no HTTP route exposes this method.
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            p, expires_at = self._pending_record(c, rid)
            if (
                not p
                or expires_at <= time.time()
                or not p["binding"]
                or p["params"]["redirect_uri"] != expected_redirect
            ):
                raise ValueError(
                    "Request missing, browser unbound, or redirect changed"
                )
            if not p["approved"]:
                p["approved"] = True
                self.put(c, "pending", rid, p, expires_at)
        self._event("approved_waiting_browser", rid=rid)
        return {
            "approved": True,
            "client": p["client_name"],
            "redirect": expected_redirect,
        }

    def _totp_secret(self):
        return read_secret(self.totp_file)

    def _check_totp(self, c, code):
        return check_totp(self, c, code, now=time.time())

    async def consent_status(self, request):
        """Read approval progress only for the browser already bound to this request."""
        rid = request.path_params["rid"]
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if len(rid) > 100:
            return JSONResponse({"error": "invalid_request"}, 400, headers=headers)
        browser = request.cookies.get(self._cookie_name(rid), "")
        with self.connect() as c:
            p, expires_at = self._pending_record(c, rid)
        if (not browser or not p or not p["binding"]
                or not hmac.compare_digest(p["binding"], digest(browser))):
            self._event("status_browser_rejected", 403, rid=rid)
            return JSONResponse({"error": "browser_mismatch"}, 403, headers=headers)
        status = ("expired" if expires_at <= time.time()
                  else "approved" if p["approved"] else "pending")
        if status != "pending":
            self._event("status_" + status, 200, rid=rid)
        return JSONResponse({"status": status, "expires_at": expires_at}, headers=headers)

    async def consent(self, request):
        rid = request.path_params["rid"]
        if len(rid) > 100:
            return HTMLResponse("Invalid request", 400)
        cookie_name = self._cookie_name(rid)
        browser = request.cookies.get(cookie_name, "")
        script_nonce = secrets.token_urlsafe(18)
        headers = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; "
                f"script-src 'nonce-{script_nonce}'; connect-src 'self'; "
                "form-action 'self'; frame-ancestors 'none'"
            ),
        }
        form_code = None
        if request.method == "POST":
            form = await request.form()
            form_code = str(form.get("code", ""))
        error = ""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            p, expires_at = self._pending_record(c, rid)
            if not p or expires_at <= time.time():
                self._event("consent_expired", 400, rid=rid)
                return HTMLResponse(
                    "授权请求已过期，请回到客户端重新连接。", 400, headers=headers
                )
            if p["binding"] is None and request.method == "GET":
                browser = secrets.token_urlsafe(32)
                p["binding"] = digest(browser)
                self.put(c, "pending", rid, p, expires_at)
                self._event("browser_bound", 200, rid=rid)
            elif not p["binding"] or not hmac.compare_digest(p["binding"], digest(browser)):
                self._event("consent_browser_rejected", 403, rid=rid)
                return HTMLResponse(
                    "这个授权请求属于另一个浏览器。", 403, headers=headers
                )
            if form_code is not None and not p["approved"]:
                ok, error = self._check_totp(c, form_code)
                if ok:
                    p["approved"] = True
                    self.put(c, "pending", rid, p, expires_at)
                    self._event("otp_accepted", 200, rid=rid)
                else:
                    self._event("otp_rejected", 400, rid=rid)
            # A successful form POST must terminate on this origin before any external
            # callback navigation.  With CSP form-action 'self', sending the POST
            # directly through a cross-origin 303 can leave the browser on the
            # consumed consent URL instead of reaching the OAuth client.  Keep the
            # approval pending, return a tiny same-origin transition page, then start
            # a fresh GET navigation from nonce-authorized script (or the fallback
            # same-origin link).  The GET path below remains the only place that
            # creates and consumes the one-time authorization code.
            if p["approved"] and request.method == "POST":
                continue_path = self.consent_path + rid  # The public proxy prefix is stripped before ASGI dispatch.
                transition = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>正在返回客户端</title>
<p>验证通过，正在返回客户端…</p>
<p><a id="continue" href="{html.escape(continue_path, quote=True)}">继续</a></p>
<script nonce="{script_nonce}">location.replace({json.dumps(continue_path)});</script>"""
                return HTMLResponse(transition, 200, headers=headers)
            if p["approved"]:
                params = AuthorizationParams.model_validate(p["params"])
                code = secrets.token_urlsafe(40)
                ac = AuthorizationCode(
                    code="",
                    client_id=p["client_id"],
                    scopes=params.scopes or [self.scope],
                    expires_at=time.time() + 120,
                    code_challenge=params.code_challenge,
                    redirect_uri=params.redirect_uri,
                    redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                    resource=self.resource,
                    subject="owner",
                )
                self.put(c, "code", code, {
                    **ac.model_dump(mode="json"), "request_id_hash": digest(rid)[:16],
                }, ac.expires_at)
                c.execute(
                    "DELETE FROM objects WHERE kind='pending' AND key=?", (digest(rid),)
                )
                response = RedirectResponse(
                    construct_redirect_uri(
                        str(params.redirect_uri), code=code, state=params.state
                    ),
                    302,
                    headers=headers,
                )
                response.delete_cookie(
                    cookie_name, path=self.consent_path, secure=True, httponly=True
                )
                self._event("callback_code_issued", 302, rid=rid)
                return response
        has_totp = bool(self._totp_secret())
        name = html.escape(self.resource_name)
        action = (
            f"""<form method="post"><label>验证器上「{html.escape(os.environ.get("OWNER_TOTP_LABEL", TOTP_LABEL))}」的 6 位码</label>
<input name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9 ]*" maxlength="7" autofocus required>
<button type="submit">确认连接</button></form>
<p class="err">{html.escape(error)}</p>
<p class="small">手边没有验证器时，也可以让 已认证的本人助手批准（授权请求：<code>{html.escape(rid)}</code>），批准后本页会自动返回客户端。</p>"""
            if has_totp
            else f"<p>此请求需要本人批准。批准后自动返回客户端。</p><p>授权请求：<code>{html.escape(rid)}</code></p>"
        )
        # Polling never refreshes a pending page, so it preserves an unfinished OTP.
        progress_script = """(() => {
  const statusUrl = location.pathname.replace(/\\/+$/, '') + '/status';
  const progress = document.getElementById('consent-progress');
  let timer, checking = false, stopped = false;
  function finish(message) {
    stopped = true;
    clearTimeout(timer);
    progress.textContent = message;
    document.querySelectorAll('input, button').forEach(el => { el.disabled = true; });
  }
  async function checkStatus() {
    if (stopped || checking) return;
    checking = true;
    try {
      const response = await fetch(statusUrl, {credentials: 'same-origin', cache: 'no-store'});
      if (stopped) return;
      if (response.status === 403) {
        finish('此授权页已失效，请回到客户端重新连接。');
        return;
      }
      if (!response.ok) return;
      const result = await response.json();
      if (stopped) return;
      if (result.status === 'approved') {
        stopped = true;
        progress.textContent = '已批准，正在返回客户端…';
        location.reload();
      } else if (result.status === 'expired') {
        finish('授权请求已过期，请回到客户端重新连接。');
      }
    } catch (_) {
      // A transient network failure does not discard input or approval.
    } finally {
      checking = false;
      if (!stopped) timer = setTimeout(checkStatus, 3000);
    }
  }
  function resume() {
    if (!document.hidden && !stopped) {
      clearTimeout(timer);
      checkStatus();
    }
  }
  document.addEventListener('visibilitychange', resume);
  document.addEventListener('resume', resume);
  window.addEventListener('pageshow', resume);
  document.querySelector('form')?.addEventListener('submit', () => {
    stopped = true;
    clearTimeout(timer);
  });
  timer = setTimeout(checkStatus, 3000);
})();"""
        body = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} — 本人授权</title><style>body{{font:18px system-ui;max-width:680px;margin:8vh auto;padding:24px;line-height:1.7;background:#101c22;color:#e6f2f1}}
code{{overflow-wrap:anywhere;color:#73ded0}}input{{font:28px ui-monospace,monospace;letter-spacing:6px;width:9em;padding:8px 12px;margin:8px 0;border-radius:8px;border:1px solid #3c5a61;background:#0b1418;color:#e6f2f1}}
button{{font:18px system-ui;padding:10px 22px;margin-left:8px;border:0;border-radius:8px;background:#2f9e8f;color:#fff}}.err{{color:#ff8f7a}}.small{{font-size:14px;color:#9bb3b0}}label{{display:block}}</style>
<h1>连接你的 {name}</h1>
<p>客户端：{html.escape(p["client_name"] or "MCP client")}</p>
<p>返回地址：<code>{html.escape(p["params"]["redirect_uri"])}</code></p>
<p>权限范围：{html.escape(self.scope)}{("；" + html.escape(self.scope_desc)) if self.scope_desc else ""}</p>
{action}
<p id="consent-progress" class="small" role="status" aria-live="polite">正在等待本人确认…</p>
<script nonce="{script_nonce}">{progress_script}</script>"""
        response = HTMLResponse(body, status_code=400 if error else 200, headers=headers)
        response.set_cookie(
            cookie_name,
            browser,
            max_age=max(1, int(expires_at - time.time())),
            path=self.consent_path,
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    async def load_authorization_code(self, client, authorization_code):
        v = self.get("code", authorization_code)
        return (
            AuthorizationCode.model_validate({**v, "code": authorization_code})
            if v
            else None
        )

    def issue(self, c, client_id, scopes, family=None):
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(40), secrets.token_urlsafe(48)
        family = family or secrets.token_hex(16)
        common = {
            "client_id": client_id,
            "scopes": scopes,
            "resource": self.resource,
            "subject": "owner",
            "family": family,
        }
        self.put(c, "access", access, {**common, "expires_at": now + 3600}, now + 3600)
        self.put(
            c,
            "refresh",
            refresh,
            {**common, "expires_at": now + 30 * 86400},
            now + 30 * 86400,
        )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            token_type="Bearer",
            expires_in=3600,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client, authorization_code):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            v = self.get("code", authorization_code.code, c=c)
            if not v or v["client_id"] != client.client_id:
                raise TokenError("invalid_grant", "Code used or expired")
            c.execute(
                "DELETE FROM objects WHERE kind='code' AND key=?",
                (digest(authorization_code.code),),
            )
            return self.issue(c, client.client_id, v["scopes"])

    async def load_refresh_token(self, client, refresh_token):
        # A spent token must reach exchange_refresh_token so the SDK cannot
        # short-circuit replay detection before the active family is revoked.
        v = self.get("refresh", refresh_token) or self.get("spent_refresh", refresh_token)
        if not v or v["client_id"] != client.client_id or v["resource"] != self.resource:
            return None
        return RefreshToken.model_validate({**v, "token": refresh_token}) if v else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        error = None
        result = None
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("DELETE FROM objects WHERE kind='spent_refresh' AND expires<=?", (time.time(),))
            v = self.get("refresh", refresh_token.token, c=c)
            if (
                not v
                or v["client_id"] != client.client_id
                or v["resource"] != self.resource
                or not set(scopes).issubset(v["scopes"])
            ):
                spent = self.get("spent_refresh", refresh_token.token, c=c)
                if (spent and spent["client_id"] == client.client_id
                        and spent["resource"] == self.resource):
                    self._revoke_family(c, spent["family"])
                error = TokenError("invalid_grant", "Refresh token used or expired")
            else:
                # put() hashes the old token key. Metadata contains no token;
                # retain only its original, bounded expiry and family binding.
                self.put(c, "spent_refresh", refresh_token.token, v, v["expires_at"])
                c.execute(
                    "DELETE FROM objects WHERE kind='refresh' AND key=?",
                    (digest(refresh_token.token),),
                )
                result = self.issue(c, client.client_id, scopes, v["family"])
        # Raise only after the revocation transaction commits; raising inside
        # connect() would roll it back and leave the attacker family active.
        if error:
            raise error
        return result

    async def load_access_token(self, token):
        v = self.get("access", token)
        return (
            AccessToken.model_validate({**v, "token": token})
            if v and v["resource"] == self.resource and self.scope in v["scopes"]
            else None
        )

    async def revoke_token(self, token):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            v = (self.get("access", token.token, c=c)
                 or self.get("refresh", token.token, c=c)
                 or self.get("spent_refresh", token.token, c=c))
            if v and v["resource"] == self.resource:
                self._revoke_family(c, v["family"])

    def _revoke_family(self, c, family):
        for kind, key, value in c.execute(
            "SELECT kind,key,value FROM objects WHERE kind IN ('access','refresh','spent_refresh')"
        ).fetchall():
            if json.loads(value)["family"] == family:
                c.execute("DELETE FROM objects WHERE kind=? AND key=?", (kind, key))

    def routes(self):
        routes = create_auth_routes(
            self,
            AnyHttpUrl(self.issuer),
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[self.scope], default_scopes=[self.scope]
            ),
            revocation_options=RevocationOptions(enabled=True),
        )

        async def resource_metadata(request):
            return JSONResponse(
                {
                    "resource": self.resource,
                    "authorization_servers": [self.issuer],
                    "scopes_supported": [self.scope],
                    "resource_name": self.resource_name,
                }
            )

        routes += [
            Route("/consent/{rid}/status", self.consent_status, methods=["GET"]),
            Route("/consent/{rid}", self.consent, methods=["GET", "POST"]),
            Route("/.well-known/oauth-protected-resource", resource_metadata),
        ]
        return routes


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default=str(DEFAULT_STATE))
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--approve", required=True)
    parser.add_argument("--redirect", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            OwnerOAuth(args.state, args.issuer).approve(args.approve, args.redirect),
            ensure_ascii=False,
        )
    )


class OAuthGuard:
    """Bound public authorization traffic and validate token resource indicators."""

    def __init__(
        self,
        app,
        provider,
        *,
        rate_limit: int | None = None,
        body_limit: int | None = None,
    ):
        from collections import deque

        self.app, self.provider, self.recent = app, provider, deque()
        self.rate_limit = rate_limit
        self.body_limit = body_limit

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "").removeprefix(scope.get("root_path", ""))
        if scope["type"] != "http" or path not in {
            "/token",
            "/register",
            "/authorize",
            "/revoke",
        }:
            return await self.app(scope, receive, send)
        now = time.monotonic()
        while self.recent and self.recent[0] < now - 60:
            self.recent.popleft()
        if self.rate_limit is not None and len(self.recent) >= self.rate_limit:
            return await JSONResponse(
                {"error": "temporarily_unavailable"}, 429, headers={"Retry-After": "60"}
            )(scope, receive, send)
        self.recent.append(now)
        if scope.get("method") == "POST":
            from urllib.parse import parse_qs, urlencode

            body = b""
            while True:
                part = await receive()
                if part["type"] == "http.disconnect":
                    return
                body += part.get("body", b"")
                if self.body_limit is not None and len(body) > self.body_limit:
                    return await JSONResponse({"error": "request_too_large"}, 413)(
                        scope, receive, send
                    )
                if not part.get("more_body"):
                    break
            if path in {"/token", "/revoke"}:
                form = parse_qs(
                    body.decode("utf-8", errors="replace"), keep_blank_values=True
                )
                if "resource" in form and form["resource"] != [self.provider.resource]:
                    return await JSONResponse({"error": "invalid_target"}, 400)(
                        scope, receive, send
                    )
                if path == "/revoke" and "client_secret" not in form:
                    # SDK v1's revocation model requires this optional field.
                    form["client_secret"] = [""]
                    body = urlencode(form, doseq=True).encode()
            sent = False

            async def replay():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return await self.app(scope, replay, send)
        await self.app(scope, receive, send)
