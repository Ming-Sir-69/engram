"""Optional official-SDK HTTP/SSE transport for the shared Engram read/write tools."""

from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import (
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from engram.mcp.oauth import is_funnel_request
from engram.mcp.profile import get_profile
from engram.mcp.server import SERVER_VERSION, _call
from engram.mcp.tools import ToolContext, tool_descriptors


def _route_path(scope):
    """Match the route seen by Starlette for both stripped and mounted prefixes."""
    path = scope.get("path", "")
    root = scope.get("root_path", "")
    if root and (path == root or path.startswith(root + "/")):
        return path[len(root):] or "/"
    return path


def create_app(
    *,
    data_dir: str | Path | None = None,
    offline: bool = False,
    profile: str = "default",
    host: str = "127.0.0.1",
    token: str | None = None,
    public_url: str | None = None,
    loopback_no_auth: bool = False,
    max_in_flight: int | None = None,
    queue_limit: int | None = None,
    request_timeout: float | None = None,
    oauth_rate_limit: int | None = None,
    oauth_body_limit: int | None = None,
    oauth_state: str | Path | None = None,
    public_path: str = "",
    token_over_funnel: bool = False,
):
    """Fail closed unless authentication or explicit loopback-only access is set.

    A token is for clients with custom headers. It is not an OAuth substitute for
    hosted clients; use the built-in owner OAuth when public_url is configured.
    By default the static token is refused on Tailscale Funnel (public) requests,
    so a static header token does not implicitly authorize public ingress.
    """
    selected = get_profile(profile)
    if loopback_no_auth and (host not in {"127.0.0.1", "::1"} or public_url):
        raise ValueError("免认证仅允许明确的本机回环地址，不能设置 public-url")
    if not token and not loopback_no_auth:
        raise ValueError("请在本机设置 ENGRAM_MCP_TOKEN；默认拒绝启动未认证服务")
    if token and (len(token) < 32 or any(c.isspace() for c in token)):
        raise ValueError("ENGRAM_MCP_TOKEN 必须至少 32 字符，且不含空白")
    if max_in_flight is not None and max_in_flight < 1:
        raise ValueError("max_in_flight 必须为正数，或使用 None 表示不限制")
    if queue_limit is not None and queue_limit < 0:
        raise ValueError("queue_limit 不能为负数，或使用 None 表示不限制")
    if request_timeout is not None and request_timeout <= 0:
        raise ValueError("request_timeout 必须为正数，或使用 None 表示不限制")
    if oauth_rate_limit is not None and oauth_rate_limit < 1:
        raise ValueError("oauth_rate_limit 必须为正数，或使用 None 表示不限制")
    if oauth_body_limit is not None and oauth_body_limit < 1:
        raise ValueError("oauth_body_limit 必须为正数，或使用 None 表示不限制")
    if public_path and (
        not public_path.startswith("/")
        or public_path.endswith("/")
        or ".." in public_path
    ):
        raise ValueError("Invalid public path")
    if oauth_state and not public_url:
        raise ValueError("OAuth requires a public HTTPS URL")
    oauth = None
    if oauth_state:
        from engram.mcp.oauth import OwnerOAuth

        oauth = OwnerOAuth(
            oauth_state,
            public_url.rstrip("/") + public_path,
            resource_name=selected.title,
        )
    allowed_hosts = [
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "[::1]",
        "[::1]:*",
    ]
    origins = ["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"]
    if public_url:
        url = urlsplit(public_url)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError("public-url 必须为不含路径、凭据和参数的 HTTPS 站点地址")
        allowed_hosts.append(url.netloc)
        origins.append(f"https://{url.netloc}")
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=origins,
    )
    icons = (
        [
            types.Icon(
                src=public_url.rstrip("/") + public_path + "/icon.svg",
                mimeType="image/svg+xml",
            )
        ]
        if public_url
        else None
    )
    server = Server(
        selected.server_name,
        version=SERVER_VERSION,
        instructions=selected.instructions,
        icons=icons,
    )

    @server.list_tools()
    async def list_tools():
        items = tool_descriptors()
        if oauth:
            for item in items:
                item["_meta"] = {
                    "securitySchemes": [{"type": "oauth2", "scopes": ["engram:access"]}]
                }
        return [types.Tool(**item) for item in items]

    def invoke(name, arguments):
        context = ToolContext.open(data_dir=data_dir, offline=offline)
        context.client_profile = selected.key
        try:
            return types.CallToolResult(
                **_call(context, {"name": name, "arguments": arguments})
            )
        finally:
            context.repository.connection.close()

    @server.call_tool()
    async def call_tool(name, arguments):
        # Each invocation owns its SQLite connection; do not add an application
        # concurrency gate here. SQLite still arbitrates conflicting writes.
        return await anyio.to_thread.run_sync(invoke, name, arguments)

    manager = StreamableHTTPSessionManager(
        server,
        stateless=True,
        json_response=True,
        security_settings=security,
    )
    sse = SseServerTransport(public_path + "/messages/", security_settings=security)
    validator = TransportSecurityMiddleware(security)

    async def handle_sse(request):
        # SDK connect_sse sends an error then raises on invalid Origin. Validate
        # before entering it, so Starlette does not try to send a second response.
        error = await validator.validate_request(request, is_post=False)
        if error:
            return error
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(*streams, server.create_initialization_options())
        return Response()

    async def health(request):
        return JSONResponse(
            {"service": selected.server_name, "transport": "http", "ok": True}
        )

    async def icon(request):
        return FileResponse(Path(__file__).parent / "assets" / "engram.svg")

    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    app = Starlette(
        routes=(oauth.routes() if oauth else [])
        + [
            Route("/healthz", health),
            Route("/icon.svg", icon),
            Route("/icon.png", icon),  # Compatibility URL, now served as image/svg+xml.
            Route("/sse", handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
            # Route accepts an ASGI callable object (not a request endpoint function).
            Route("/mcp", endpoint=HTTPTransport(manager)),
        ],
        lifespan=lifespan,
    )
    if any(
        value is not None for value in (max_in_flight, queue_limit, request_timeout)
    ):
        app = AdmissionControl(
            app,
            max_in_flight=max_in_flight,
            queue_limit=queue_limit,
            request_timeout=request_timeout,
        )
    app = BearerAuth(app, token, oauth, token_over_funnel=token_over_funnel)
    if oauth:
        from engram.mcp.oauth import OAuthGuard

        app = OAuthGuard(
            app,
            oauth,
            rate_limit=oauth_rate_limit,
            body_limit=oauth_body_limit,
        )
    return app


class AdmissionControl:
    """Bound concurrent MCP requests and fail fast when the queue is full."""

    def __init__(
        self,
        app,
        *,
        max_in_flight: int | None,
        queue_limit: int | None,
        request_timeout: float | None,
    ):
        self.app = app
        self.limiter = (
            anyio.CapacityLimiter(max_in_flight) if max_in_flight is not None else None
        )
        self.queue_limit = queue_limit
        self.request_timeout = request_timeout
        self._state_lock = anyio.Lock()
        self._waiting = 0

    async def _try_acquire(self) -> bool:
        if self.limiter is None:
            return True
        try:
            self.limiter.acquire_nowait()
            return True
        except anyio.WouldBlock:
            async with self._state_lock:
                if self.queue_limit is not None and self._waiting >= self.queue_limit:
                    return False
                self._waiting += 1
            try:
                await self.limiter.acquire()
                return True
            finally:
                async with self._state_lock:
                    self._waiting -= 1

    async def __call__(self, scope, receive, send):
        path = _route_path(scope)
        if scope.get("type") != "http" or not (
            path in {"/mcp", "/sse"} or path.startswith("/messages/")
        ):
            await self.app(scope, receive, send)
            return
        acquired = await self._try_acquire()
        if not acquired:
            await JSONResponse(
                {"error": "busy", "detail": "MCP request queue is full"},
                status_code=429,
                headers={"Retry-After": "1"},
            )(scope, receive, send)
            return
        try:
            if self.request_timeout is None:
                await self.app(scope, receive, send)
            else:
                with anyio.fail_after(self.request_timeout):
                    await self.app(scope, receive, send)
        except TimeoutError:
            await JSONResponse(
                {"error": "timeout", "detail": "MCP request exceeded its deadline"},
                status_code=504,
            )(scope, receive, send)
        finally:
            if self.limiter is not None:
                self.limiter.release()


class HTTPTransport:
    def __init__(self, manager):
        self.manager = manager

    async def __call__(self, scope, receive, send):
        await self.manager.handle_request(scope, receive, send)


class BearerAuth:
    def __init__(self, app, token, oauth=None, *, token_over_funnel=False):
        self.app, self.token, self.oauth = app, token, oauth
        self.token_over_funnel = token_over_funnel

    async def __call__(self, scope, receive, send):
        path = _route_path(scope)
        if (
            scope["type"] == "http"
            and self.token
            and (
                path in {"/mcp", "/sse"}
                or path.startswith("/messages/")
            )
        ):
            headers = dict(scope.get("headers", []))
            expected = ("Bearer " + self.token).encode()
            supplied = headers.get(b"authorization", b"")
            public = is_funnel_request(headers) and not self.token_over_funnel
            valid = hmac.compare_digest(supplied, expected) and not public
            if not valid and self.oauth and supplied.startswith(b"Bearer "):
                value = supplied[7:].decode("utf-8", errors="replace")
                valid = await self.oauth.load_access_token(value) is not None
            if not valid:
                await JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={
                        "WWW-Authenticate": (
                            f'Bearer resource_metadata="{self.oauth.issuer}/.well-known/oauth-protected-resource", scope="engram:access"'
                            if self.oauth
                            else "Bearer"
                        )
                    },
                )(scope, receive, send)
                return
        await self.app(scope, receive, send)


def run_remote(*, host="127.0.0.1", port=8768, profile="default", **kwargs):
    selected = get_profile(profile)
    token = os.environ.get("ENGRAM_MCP_TOKEN")
    token_file = os.environ.get("ENGRAM_MCP_TOKEN_FILE")
    if not token and token_file:
        token = Path(token_file).read_text(encoding="utf-8").strip()

    def optional_number(name: str, *, cast):
        value = os.environ.get(name)
        if value is None or value.strip().lower() in {
            "",
            "0",
            "none",
            "off",
            "unlimited",
            "infinite",
        }:
            return None
        return cast(value)

    app = create_app(
        host=host,
        profile=selected.key,
        token=token,
        oauth_state=os.environ.get("ENGRAM_MCP_OAUTH_STATE"),
        public_path=os.environ.get("ENGRAM_MCP_PUBLIC_PATH", ""),
        max_in_flight=optional_number("ENGRAM_MCP_MAX_IN_FLIGHT", cast=int),
        queue_limit=optional_number("ENGRAM_MCP_QUEUE_LIMIT", cast=int),
        request_timeout=optional_number("ENGRAM_MCP_REQUEST_TIMEOUT", cast=float),
        oauth_rate_limit=optional_number("ENGRAM_MCP_OAUTH_RATE_LIMIT", cast=int),
        oauth_body_limit=optional_number("ENGRAM_MCP_OAUTH_BODY_LIMIT", cast=int),
        token_over_funnel=os.environ.get("ENGRAM_MCP_TOKEN_OVER_FUNNEL") == "1",
        **kwargs,
    )
    shared = None
    # The default online service owns 8772. A Claude companion must only reuse
    # that endpoint; isolated offline transports must not open a model listener.
    if selected.key == "default" and not kwargs.get("offline", False):
        from engram.config import load_config
        from engram.embedding import MLXEmbedder
        from engram.embedding_server import start_embedding_server

        config = load_config(data_dir=kwargs.get("data_dir"))
        shared = start_embedding_server(
            MLXEmbedder(
                model_path=config.model_path(config.embedding_model),
                dimensions=config.embedding_dimensions,
            )
        )
    try:
        uvicorn.run(app, host=host, port=port, access_log=False, log_level="warning")
    finally:
        if shared is not None:
            shared.shutdown()
            shared.server_close()
