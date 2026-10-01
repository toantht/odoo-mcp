"""Phase 12 tests: `gateway/http.py`'s OAuth wiring.

No real Odoo, no subprocess (unlike `scripts/smoke_mcp_http.py`) - the
ASGI app `create_app()` builds is exercised in-process via
`httpx.ASGITransport`, and the per-request API key resolution
(`_build_mcp_servers`'s `_backend_factory`) is exercised directly against
`mcp.server.auth.middleware.auth_context.auth_context_var`, standing in
for what `RequireAuthMiddleware`/`AuthContextMiddleware` would have set
up for a real authenticated request (the plan's "tool call dùng key
trong vault", backend giả via a monkeypatched `build_backend`).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from odoo_mcp.auth.provider import OdooAccessToken
from odoo_mcp.backends.fake import FakeBackend
from odoo_mcp.config.registry import ServerConfig
from odoo_mcp.gateway import http as http_module
from odoo_mcp.gateway.http import _transport_security

_PUBLIC_URL = "http://127.0.0.1"


def _write_registry(tmp_path: Path) -> Path:
    path = tmp_path / "servers.yaml"
    path.write_text(
        "servers:\n  odoo_a:\n    url: https://odoo-a.example.com\n"
        "    version: 19\n    backend: json2\n",
        encoding="utf-8",
    )
    return path


def _set_oauth_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ODOO_MCP_CONFIG", str(_write_registry(tmp_path)))
    monkeypatch.delenv("ODOO_MCP_ALLOW_SHARED_KEY", raising=False)
    monkeypatch.setenv("ODOO_MCP_PUBLIC_URL", _PUBLIC_URL)
    monkeypatch.setenv("ODOO_MCP_VAULT_KEY", "vault-secret")
    monkeypatch.setenv("ODOO_MCP_VAULT_PATH", str(tmp_path / "vault.sqlite3"))
    monkeypatch.setenv("ODOO_MCP_CHANNEL_SECRET_ODOO_A", "channel-secret")


async def _get(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=_PUBLIC_URL) as client:
        return await client.get(path)


def test_healthz_reports_oauth_enabled_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_oauth_env(monkeypatch, tmp_path)

    app = http_module.create_app()
    response = asyncio.run(_get(app, "/healthz"))

    assert response.status_code == 200
    body = response.json()
    assert body["oauth_enabled"] is True
    assert body["servers"] == ["odoo_a"]


def test_authorization_server_metadata_is_shared_across_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_oauth_env(monkeypatch, tmp_path)

    app = http_module.create_app()
    response = asyncio.run(_get(app, "/.well-known/oauth-authorization-server"))

    assert response.status_code == 200
    body = response.json()
    assert body["authorization_endpoint"] == f"{_PUBLIC_URL}/authorize"
    assert body["token_endpoint"] == f"{_PUBLIC_URL}/token"
    assert body["registration_endpoint"] == f"{_PUBLIC_URL}/register"
    assert "S256" in body["code_challenge_methods_supported"]


def test_protected_resource_metadata_points_at_shared_issuer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_oauth_env(monkeypatch, tmp_path)

    app = http_module.create_app()
    response = asyncio.run(_get(app, "/.well-known/oauth-protected-resource/mcp/odoo_a"))

    assert response.status_code == 200
    body = response.json()
    assert body["resource"] == f"{_PUBLIC_URL}/mcp/odoo_a"
    assert body["authorization_servers"] == [f"{_PUBLIC_URL}/"]


def test_mcp_endpoint_without_bearer_token_is_401_with_resource_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_oauth_env(monkeypatch, tmp_path)

    app = http_module.create_app()
    response = asyncio.run(_get(app, "/mcp/odoo_a"))

    assert response.status_code == 401
    challenge = response.headers.get("www-authenticate", "")
    assert "resource_metadata=" in challenge
    assert "/mcp/odoo_a" in challenge


def test_mcp_endpoint_unknown_server_is_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_oauth_env(monkeypatch, tmp_path)

    app = http_module.create_app()
    response = asyncio.run(_get(app, "/mcp/does_not_exist"))

    assert response.status_code == 404


def test_create_app_requires_public_url_when_oauth_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The real project `.env` may itself set ODOO_MCP_PUBLIC_URL (e.g. a
    # manual pilot test session's tunnel URL) - `load_dotenv()`'s default
    # `override=False` would otherwise silently fill the var back in
    # right after `delenv` below, from that file, not this test's env.
    monkeypatch.setattr(http_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("ODOO_MCP_CONFIG", str(_write_registry(tmp_path)))
    monkeypatch.delenv("ODOO_MCP_ALLOW_SHARED_KEY", raising=False)
    monkeypatch.delenv("ODOO_MCP_PUBLIC_URL", raising=False)

    with pytest.raises(RuntimeError, match="ODOO_MCP_PUBLIC_URL"):
        http_module.create_app()


def test_shared_key_mode_skips_oauth_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ODOO_MCP_ALLOW_SHARED_KEY=1` is the Phase 2-11 back-compat path -
    no OAuth routes, no vault, `/mcp/{id}` reachable without a bearer
    token (the shared ODOO_API_KEY is used server-side instead)."""
    monkeypatch.setenv("ODOO_MCP_CONFIG", str(_write_registry(tmp_path)))
    monkeypatch.setenv("ODOO_MCP_ALLOW_SHARED_KEY", "1")
    monkeypatch.delenv("ODOO_MCP_PUBLIC_URL", raising=False)
    monkeypatch.setenv("ODOO_API_KEY", "shared-key")

    app = http_module.create_app()

    health = asyncio.run(_get(app, "/healthz"))
    assert health.json()["oauth_enabled"] is False

    metadata = asyncio.run(_get(app, "/.well-known/oauth-authorization-server"))
    assert metadata.status_code == 404


def test_transport_security_always_allows_localhost(tmp_path: Path) -> None:
    settings = _transport_security(None)

    assert settings.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in settings.allowed_hosts
    assert "localhost:*" in settings.allowed_hosts


def test_transport_security_allows_the_configured_public_url_host(tmp_path: Path) -> None:
    """Regression test: a real Odoo 19 pilot test through a `cloudflared`/
    `ngrok` tunnel hit `421 Misdirected Request` on every `/mcp/{id}` call
    (`mcp.server.transport_security: Invalid Host header: <tunnel domain>`)
    - `FastMCP` auto-enables its own Host/Origin allowlist for DNS-
    rebinding protection and only ever allowed `127.0.0.1`/`localhost` by
    default (see `_transport_security`'s docstring). The gateway's own
    OAuth bearer-token check had already passed by that point - this is
    a *different*, lower (MCP-transport) layer that also has to know
    about the public tunnel/reverse-proxy host, not just Starlette's
    routes."""
    settings = _transport_security("https://receipt-strange-indicate-indices.trycloudflare.com")

    assert "receipt-strange-indicate-indices.trycloudflare.com" in settings.allowed_hosts
    assert "https://receipt-strange-indicate-indices.trycloudflare.com" in settings.allowed_origins
    # still keeps the localhost defaults too, for local (no-tunnel) testing
    assert "127.0.0.1:*" in settings.allowed_hosts


def test_build_mcp_servers_wires_transport_security_from_public_url(tmp_path: Path) -> None:
    registry = {
        "odoo_a": ServerConfig(id="odoo_a", url="https://odoo-a.example.com", db=None, version=19, backend="json2")
    }

    servers = http_module._build_mcp_servers(
        registry, oauth_enabled=True, public_url="https://gw.example.com"
    )

    security = servers["odoo_a"].settings.transport_security
    assert security is not None
    assert "gw.example.com" in security.allowed_hosts


def test_backend_factory_uses_the_calling_users_vault_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core of Phase 12's HTTP wiring: with OAuth enabled, a tool
    call's `Backend` is built with *that request's* resolved API key
    (`get_access_token()`), never a shared one - exercised directly
    against the auth contextvar so this does not depend on driving a
    full MCP JSON-RPC session over the ASGI transport."""
    captured: dict[str, object] = {}

    def fake_build_backend(cfg: ServerConfig, *, api_key: str):
        captured["server_id"] = cfg.id
        captured["api_key"] = api_key
        return FakeBackend()

    monkeypatch.setattr(http_module, "build_backend", fake_build_backend)

    registry = {
        "odoo_a": ServerConfig(
            id="odoo_a",
            url="https://odoo-a.example.com",
            db=None,
            version=19,
            backend="json2",
        )
    }
    servers = http_module._build_mcp_servers(registry, oauth_enabled=True)
    search_read = servers["odoo_a"]._tool_manager.get_tool("search_read")
    assert search_read is not None

    token = OdooAccessToken(
        token="access-1",
        client_id="client-1",
        scopes=["mcp"],
        expires_at=None,
        resource=None,
        subject="odoo_a:7",
        server_id="odoo_a",
        odoo_uid=7,
        odoo_login="alice@example.com",
        api_key="ALICE-API-KEY",
    )
    reset_token = auth_context_var.set(AuthenticatedUser(token))
    try:
        search_read.fn(model="res.partner", fields=["id", "name"], limit=20)
    finally:
        auth_context_var.reset(reset_token)

    assert captured == {"server_id": "odoo_a", "api_key": "ALICE-API-KEY"}


def test_backend_factory_rejects_token_minted_for_another_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth even though `BearerAuthBackend`'s own audience
    check (`resource_server_url`) should already have rejected this at
    the HTTP layer - a token for 'odoo_a' must never resolve a Backend
    for 'odoo_b' just because both share one gateway process."""
    registry = {
        "odoo_a": ServerConfig(
            id="odoo_a",
            url="https://odoo-a.example.com",
            db=None,
            version=19,
            backend="json2",
        ),
        "odoo_b": ServerConfig(
            id="odoo_b",
            url="https://odoo-b.example.com",
            db=None,
            version=19,
            backend="json2",
        ),
    }
    servers = http_module._build_mcp_servers(registry, oauth_enabled=True)
    search_read = servers["odoo_b"]._tool_manager.get_tool("search_read")

    token = OdooAccessToken(
        token="access-1",
        client_id="client-1",
        scopes=["mcp"],
        expires_at=None,
        resource=None,
        subject="odoo_a:7",
        server_id="odoo_a",
        odoo_uid=7,
        odoo_login="alice@example.com",
        api_key="ALICE-API-KEY",
    )
    reset_token = auth_context_var.set(AuthenticatedUser(token))
    try:
        with pytest.raises(Exception, match="No authenticated Odoo session"):
            search_read.fn(model="res.partner", fields=["id", "name"], limit=20)
    finally:
        auth_context_var.reset(reset_token)
