"""Phase 6: HTTP gateway, one MCP endpoint per registry server.

Each server in `config/servers.yaml` (Phase 5 registry) gets its own
Streamable HTTP MCP endpoint at `/mcp/{server_id}` - a separate
connector per Odoo server, not a `server` parameter on every tool (see
the plan's "Quyết định đã chọn"). Every endpoint exposes the exact same
tools as `gateway/stdio.py` (`ping` plus the Phase 11 generic tools -
see `core.mcp_server.build_mcp_server`), just reachable over HTTP
instead of stdio, so stdio keeps working unchanged for local clients.

Every registry entry with a supported `backend:` (`json2` or, since
Phase 8, `xmlrpc`) is mounted - see `backends.build_backend` for the
dispatch.

Phase 9: each server's own `write_tools:` allowlist (`ServerConfig
.write_tools`) is passed straight through to `build_mcp_server`, so
`/mcp/odoo_a` and `/mcp/odoo_b` can each expose a different (or empty)
set of write tools - unlike the API key, this is not shared across
servers. Phase 11 does the same for `ServerConfig.allowed_models` (which
models/fields the generic tools may read/write).

Phase 12 - who a tool call runs as:
By default every mounted server requires a Claude OAuth session tied to
one Odoo user's consent (see `auth/provider.py`, `auth/vault.py`, and the
`addon/odoo_mcp` Odoo addon this all talks to) - each `/mcp/{server_id}`
call runs with *that* user's own freshly-generated Odoo API key, read
per-request from `mcp.server.auth.middleware.auth_context.get_access_token()`
(never memoized - see `core.mcp_server.build_mcp_server`'s
`cache_backend=False`). Setting `ODOO_MCP_ALLOW_SHARED_KEY=1` reverts a
server to the Phase 2-11 behaviour instead: one shared `ODOO_API_KEY` env
var for every caller, no OAuth, no addon required - kept only so
`scripts/smoke_mcp_http.py` and existing single-tenant deployments keep
working unchanged. New deployments should leave OAuth on.

Because this is reachable over a network instead of a trusted local
pipe, it adds three things `stdio.py` does not need:
- `LoggingMiddleware`: one line per `/mcp/*` request (method, path,
  server_id, JSON-RPC method, tool name for `tools/call`, status,
  client ip). The `Authorization` header/API key/MCP bearer token is
  *never* logged.
- `RateLimitMiddleware`: a simple in-memory fixed-window limiter for
  `/mcp/*` requests, keyed by the (hashed) `Authorization` header when
  present, else by client ip.
- (Phase 12, OAuth mode only) a per-server bearer-token check
  (`_wrap_with_auth`) plus the shared OAuth routes (`auth.provider
  .build_routes`) and each server's own protected-resource metadata
  (`create_protected_resource_routes`).

Run directly:
    uv run odoo-mcp-http
    uv run uvicorn --factory odoo_mcp.gateway.http:create_app

Then:
    curl http://127.0.0.1:8000/healthz
    uv run python scripts/smoke_mcp_http.py   # health + tools/list + ping
    (add ODOO_MCP_ALLOW_SHARED_KEY=1 to the environment first - the smoke
    script calls tools with a shared ODOO_API_KEY, not an OAuth flow)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from dotenv import load_dotenv
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware, get_access_token
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import ProviderTokenVerifier
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from odoo_mcp.auth.provider import OdooAccessToken, OdooMcpAuthProvider, build_routes
from odoo_mcp.auth.vault import Vault, VaultError
from odoo_mcp.backends import Json2Error, build_backend
from odoo_mcp.config.registry import ServerConfig, load_registry
from odoo_mcp.core.backend import Backend
from odoo_mcp.core.mcp_server import build_mcp_server

logger = logging.getLogger("odoo_mcp.gateway.http")

_MCP_PREFIX = "/mcp/"
_DEFAULT_RATE_LIMIT = 60
_DEFAULT_RATE_WINDOW_SECONDS = 60.0
_DEFAULT_OAUTH_REDIRECT_HOSTS = frozenset({"claude.ai", "localhost", "127.0.0.1"})


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _oauth_enabled() -> bool:
    """Phase 12 default: on. `ODOO_MCP_ALLOW_SHARED_KEY=1` opts a whole
    gateway process back into the Phase 2-11 one-shared-key model (see
    module docs) - there is no per-server toggle, a gateway either asks
    every connector to go through Odoo consent or it does not."""
    load_dotenv()
    return not _truthy(os.environ.get("ODOO_MCP_ALLOW_SHARED_KEY", ""))


def _api_key() -> str:
    """Read `ODOO_API_KEY` lazily - only used when OAuth is disabled
    (`ODOO_MCP_ALLOW_SHARED_KEY=1`), same as the Phase 2-11 gateways."""
    load_dotenv()
    api_key = os.environ.get("ODOO_API_KEY", "").strip()
    if not api_key:
        raise Json2Error("Missing ODOO_API_KEY env var (see .env.example).")
    return api_key


def _public_url() -> str:
    load_dotenv()
    url = os.environ.get("ODOO_MCP_PUBLIC_URL", "").strip()
    if not url:
        raise RuntimeError(
            "ODOO_MCP_PUBLIC_URL is required when OAuth is enabled (the "
            "default - see module docs) - the public https URL this "
            "gateway is reachable at, e.g. https://mcp.example.com. Set "
            "ODOO_MCP_ALLOW_SHARED_KEY=1 instead to skip OAuth entirely."
        )
    return url.rstrip("/")


def _channel_secret_for_server(server_id: str) -> str:
    """`ODOO_MCP_CHANNEL_SECRET_<SERVER_ID>` (server id upper-cased) -
    the same value must be set in that Odoo server's Settings > MCP
    Gateway > "MCP Channel Secret" (`odoo_mcp.channel_secret`, see
    `addon/odoo_mcp/models/res_config_settings.py`). Deliberately one env
    var per server, never in `servers.yaml` (Phase 5 rule: no secret
    lives in the registry file)."""
    load_dotenv()
    return os.environ.get(f"ODOO_MCP_CHANNEL_SECRET_{server_id.upper()}", "").strip()


def _allowed_redirect_hosts() -> set[str]:
    load_dotenv()
    extra = os.environ.get("ODOO_MCP_OAUTH_REDIRECT_HOSTS", "")
    hosts = set(_DEFAULT_OAUTH_REDIRECT_HOSTS)
    hosts.update(host.strip() for host in extra.split(",") if host.strip())
    return hosts


def _transport_security(public_url: str | None) -> TransportSecuritySettings:
    """`FastMCP` auto-enables MCP-transport DNS-rebinding protection
    (Host/Origin header allowlist) whenever it's bound to `127.0.0.1`
    (see `mcp.server.fastmcp.server.FastMCP.__init__`) - which is always,
    since `build_mcp_server` never passes a `host=`. Reached directly
    that's fine, but behind a reverse proxy/tunnel (Phase 7, or a plain
    `ngrok`/`cloudflared` tunnel while testing Phase 12) the inbound
    `Host` header is the *public* domain, not `127.0.0.1` - left at
    FastMCP's default this 421s every real request with a confusing
    "Invalid Host header" log line and no other symptom. Keep the
    protection (still worth having against an actual DNS-rebinding
    attack from a browser), just also allow `ODOO_MCP_PUBLIC_URL`'s own
    host - the one legitimate non-localhost Host/Origin this gateway
    should ever see."""
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if public_url:
        netloc = urlparse(public_url).netloc
        if netloc:
            hosts.append(netloc)
            origins.append(public_url.rstrip("/"))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def _vault() -> Vault:
    load_dotenv()
    path = os.environ.get("ODOO_MCP_VAULT_PATH", "config/vault.sqlite3")
    vault_key = os.environ.get("ODOO_MCP_VAULT_KEY", "").strip()
    if not vault_key:
        raise VaultError(
            "ODOO_MCP_VAULT_KEY is required when OAuth is enabled (the "
            "default - see module docs) - a long random secret used to "
            "encrypt Odoo API keys at rest in the vault."
        )
    return Vault(path, vault_key=vault_key)


def _build_mcp_servers(
    registry: dict[str, ServerConfig], *, oauth_enabled: bool, public_url: str | None = None
) -> dict[str, FastMCP]:
    """One `FastMCP` per registry server - skips anything with an
    unsupported `backend:` value, logging why. `json2`/`xmlrpc`-specific
    misconfiguration (e.g. xmlrpc missing `db`/`login`) is *not* checked
    here - that only surfaces as a tool error on first real call, same
    as a missing `ODOO_API_KEY` (see `build_backend`, `_backend_factory`
    below).

    `oauth_enabled=True` (the default) makes every tool call read its
    API key from the current request's resolved OAuth access token
    (`get_access_token()`, set by the auth middleware `create_app` wraps
    each `/mcp/{server_id}` route in - see `_wrap_with_auth`) instead of
    a single shared `ODOO_API_KEY`, and `cache_backend=False` so that
    lookup happens on every call, never just the first one."""
    servers: dict[str, FastMCP] = {}
    for server_id, config in registry.items():
        if config.backend not in ("json2", "xmlrpc"):
            logger.warning(
                "Skipping server '%s': backend '%s' not supported by the "
                "HTTP gateway.",
                server_id,
                config.backend,
            )
            continue

        if oauth_enabled:

            def _backend_factory(cfg: ServerConfig = config) -> Backend:
                token = get_access_token()
                if not isinstance(token, OdooAccessToken) or token.server_id != cfg.id:
                    # RequireAuthMiddleware/BearerAuthBackend already reject
                    # anything reaching this point without a valid,
                    # resource-matched token - this is defence in depth,
                    # not the normal error path a caller sees.
                    raise Json2Error("No authenticated Odoo session for this request.")
                return build_backend(cfg, api_key=token.api_key)

        else:

            def _backend_factory(cfg: ServerConfig = config) -> Backend:
                return build_backend(cfg, api_key=_api_key())

        servers[server_id] = build_mcp_server(
            f"odoo-mcp-{server_id}",
            _backend_factory,
            write_tools=config.write_tools,
            allowed_models=config.allowed_models,
            cache_backend=not oauth_enabled,
            transport_security=_transport_security(public_url),
        )
    return servers


def _wrap_with_auth(app: ASGIApp, *, token_verifier, resource_url: str) -> ASGIApp:
    """Requires a valid Bearer token (`RequireAuthMiddleware`), scoped to
    this one server's `resource_url` (`BearerAuthBackend`'s audience
    check - a token minted for `/mcp/odoo_a` is rejected on
    `/mcp/odoo_b`), and makes it available to tool calls as
    `get_access_token()` (`AuthContextMiddleware`) - see
    `_build_mcp_servers`'s `_backend_factory`. Order matters:
    `AuthenticationMiddleware` (sets `scope['user']`) must run before
    both `AuthContextMiddleware` (reads it into a contextvar) and
    `RequireAuthMiddleware` (rejects the request if it's still unset) -
    mirrors how `FastMCP` itself composes these three when `auth=` is
    passed directly to it."""
    resource_metadata_url = build_resource_metadata_url(AnyHttpUrl(resource_url))
    app = RequireAuthMiddleware(app, required_scopes=[], resource_metadata_url=resource_metadata_url)
    app = AuthContextMiddleware(app)
    app = AuthenticationMiddleware(
        app,
        backend=BearerAuthBackend(token_verifier, resource_server_url=AnyHttpUrl(resource_url)),
    )
    return app


def create_app(
    registry_path: str | None = None,
    *,
    rate_limit: int | None = None,
    rate_limit_window_seconds: float | None = None,
) -> ASGIApp:
    """Build the full HTTP gateway ASGI app: `/healthz` plus one
    `/mcp/{server_id}` per usable registry server, wrapped in the
    logging + rate-limit middleware, plus (Phase 12, unless
    `ODOO_MCP_ALLOW_SHARED_KEY=1`) the shared OAuth routes and each
    server's own protected-resource metadata + bearer check.

    Raises `RegistryError` (missing/invalid `servers.yaml`) or
    `RuntimeError` (no supported-backend servers in it, or missing OAuth
    env config) at call time, not at import time - so importing this
    module never touches the filesystem/env by itself.
    """
    registry = load_registry(registry_path)
    oauth_enabled = _oauth_enabled()
    public_url = _public_url() if oauth_enabled else None
    mcp_servers = _build_mcp_servers(registry, oauth_enabled=oauth_enabled, public_url=public_url)
    if not mcp_servers:
        raise RuntimeError(
            "No servers with a supported 'backend:' (json2/xmlrpc) found in "
            "the registry - nothing for the HTTP gateway to mount (see "
            "config/servers.example.yaml)."
        )

    async def healthz(_: Request) -> Response:
        return JSONResponse(
            {"status": "ok", "servers": sorted(mcp_servers), "oauth_enabled": oauth_enabled}
        )

    routes: list[Route] = [Route("/healthz", healthz, methods=["GET"])]

    if oauth_enabled:
        assert public_url is not None  # set above whenever oauth_enabled
        provider = OdooMcpAuthProvider(
            registry,
            _vault(),
            public_url=public_url,
            allowed_redirect_hosts=_allowed_redirect_hosts(),
            channel_secret_for_server=_channel_secret_for_server,
        )
        token_verifier = ProviderTokenVerifier(provider)
        routes.extend(
            build_routes(
                provider,
                issuer_url=AnyHttpUrl(public_url),
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, valid_scopes=["mcp"], default_scopes=["mcp"]
                ),
            )
        )
        logger.info("OAuth enabled - issuer %s", public_url)

    for server_id, mcp in mcp_servers.items():
        mcp.streamable_http_app()  # lazily creates mcp.session_manager
        endpoint: ASGIApp = StreamableHTTPASGIApp(mcp.session_manager)
        if oauth_enabled:
            resource_url = f"{public_url}/mcp/{server_id}"
            endpoint = _wrap_with_auth(endpoint, token_verifier=token_verifier, resource_url=resource_url)
            routes.extend(
                create_protected_resource_routes(
                    resource_url=AnyHttpUrl(resource_url),
                    authorization_servers=[AnyHttpUrl(public_url)],
                    scopes_supported=["mcp"],
                )
            )
        routes.append(Route(f"/mcp/{server_id}", endpoint=endpoint))
        logger.info("Mounted MCP endpoint /mcp/%s (oauth=%s)", server_id, oauth_enabled)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for mcp in mcp_servers.values():
                await stack.enter_async_context(mcp.session_manager.run())
            yield

    starlette_app = Starlette(routes=routes, lifespan=lifespan)
    return RateLimitMiddleware(
        LoggingMiddleware(starlette_app),
        limit=rate_limit or int(os.environ.get("ODOO_MCP_RATE_LIMIT", _DEFAULT_RATE_LIMIT)),
        window_seconds=rate_limit_window_seconds
        or float(os.environ.get("ODOO_MCP_RATE_LIMIT_WINDOW", _DEFAULT_RATE_WINDOW_SECONDS)),
    )


def _server_id_from_path(path: str) -> str | None:
    if not path.startswith(_MCP_PREFIX):
        return None
    return path[len(_MCP_PREFIX) :].split("/", 1)[0] or None


def _extract_rpc_info(body: bytes) -> tuple[str | None, str | None]:
    """Best-effort parse of a JSON-RPC request body for logging only:
    the `method` (e.g. "tools/call") and, for `tools/call`, the tool
    name. Never raises - returns `(None, None)` for anything unexpected
    (empty body, non-JSON, a batch, ...)."""
    if not body:
        return None, None
    try:
        payload = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    method = payload.get("method")
    tool = None
    if method == "tools/call" and isinstance(payload.get("params"), dict):
        tool = payload["params"].get("name")
    return method, tool


class LoggingMiddleware:
    """Logs one line per `/mcp/*` HTTP request: method, path, server_id,
    JSON-RPC method + tool name (for `tools/call`), response status,
    client ip. Deliberately never logs headers, so the `Authorization`
    bearer token/API key is never logged.
    """

    _MAX_BODY_BYTES = 64 * 1024

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        path = scope["path"]
        server_id = _server_id_from_path(path)
        client = scope.get("client")
        client_ip = client[0] if client else "?"

        body_chunks: list[bytes] = []
        body_len = 0

        async def receive_wrapper() -> Any:
            nonlocal body_len
            message = await receive()
            if message["type"] == "http.request" and body_len < self._MAX_BODY_BYTES:
                chunk = message.get("body", b"")
                body_chunks.append(chunk)
                body_len += len(chunk)
            return message

        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        finally:
            rpc_method, tool = _extract_rpc_info(b"".join(body_chunks))
            logger.info(
                "mcp_request method=%s path=%s server_id=%s rpc=%s tool=%s "
                "status=%s client=%s",
                method,
                path,
                server_id,
                rpc_method,
                tool,
                status_holder.get("status"),
                client_ip,
            )


class RateLimitMiddleware:
    """Simple fixed-window rate limit for `/mcp/*` requests only
    (`/healthz` is never limited). Keyed by the `Authorization` header
    (sha256 hash - the raw bearer token/API key is never kept/logged)
    when present, else by client ip.

    In-memory only, per gateway process - fine for a single instance;
    a multi-process/replica deployment would need a shared store
    (redis, ...) instead, out of scope for Phase 6.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int = _DEFAULT_RATE_LIMIT,
        window_seconds: float = _DEFAULT_RATE_WINDOW_SECONDS,
    ) -> None:
        self.app = app
        self.limit = limit
        self.window_seconds = window_seconds
        self._windows: dict[str, tuple[float, int]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(_MCP_PREFIX):
            await self.app(scope, receive, send)
            return

        key = self._rate_limit_key(scope)
        if not self._allow(key):
            logger.warning("mcp_rate_limited path=%s client=%s", scope["path"], key)
            response = JSONResponse(
                {"error": "rate_limited", "retry_after_seconds": self.window_seconds},
                status_code=429,
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _rate_limit_key(scope: Scope) -> str:
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization")
        if auth:
            digest = hashlib.sha256(auth).hexdigest()[:16]
            return f"key:{digest}"
        client = scope.get("client")
        return f"ip:{client[0]}" if client else "ip:unknown"

    def _allow(self, key: str) -> bool:
        now = time.monotonic()
        window_start, count = self._windows.get(key, (now, 0))
        if now - window_start >= self.window_seconds:
            window_start, count = now, 0
        count += 1
        self._windows[key] = (window_start, count)
        return count <= self.limit


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    load_dotenv()
    host = os.environ.get("ODOO_MCP_HTTP_HOST", "127.0.0.1")
    port = int(os.environ.get("ODOO_MCP_HTTP_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
