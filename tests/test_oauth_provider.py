"""Phase 12 unit tests: `odoo_mcp.auth.provider.OdooMcpAuthProvider`.

No network, no real Odoo, no ASGI server: exercises the provider's own
methods directly (as `mcp.server.auth.routes`'s handlers would call
them - see that package for the request parsing/PKCE-verification layer
this deliberately does not re-test). `_redeem_with_odoo` (the one method
that actually calls out to an Odoo server's `/odoo_mcp/token`) is
monkeypatched with a canned response, standing in for the addon per the
plan's "callback với token endpoint giả lập".
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from odoo_mcp.auth.provider import OdooAccessToken, OdooAuthorizationCode, OdooMcpAuthProvider
from odoo_mcp.auth.vault import Vault
from odoo_mcp.config.registry import ServerConfig


def _server(server_id: str = "odoo_a", url: str = "https://odoo-a.example.com") -> ServerConfig:
    return ServerConfig(id=server_id, url=url, db=None, version=19, backend="json2")


def _provider(tmp_path: Path, **overrides) -> OdooMcpAuthProvider:
    registry = overrides.pop("registry", {"odoo_a": _server()})
    vault = overrides.pop("vault", Vault(tmp_path / "vault.sqlite3", vault_key="vault-secret"))
    kwargs = dict(
        public_url="https://gw.example.com",
        allowed_redirect_hosts={"claude.ai"},
        channel_secret_for_server=lambda server_id: "channel-secret",
    )
    kwargs.update(overrides)
    return OdooMcpAuthProvider(registry, vault, **kwargs)


def _client(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )


def _auth_params(**overrides) -> AuthorizationParams:
    kwargs = dict(
        state="state-1",
        scopes=["mcp"],
        code_challenge="challenge-1",
        redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
        redirect_uri_provided_explicitly=True,
        resource="https://gw.example.com/mcp/odoo_a",
    )
    kwargs.update(overrides)
    return AuthorizationParams(**kwargs)


def test_authorize_redirects_to_odoo_addon(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    url = asyncio.run(provider.authorize(_client(), _auth_params()))

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == (
        "https://odoo-a.example.com/odoo_mcp/authorize"
    )
    query = parse_qs(parsed.query)
    assert query["redirect_uri"] == ["https://gw.example.com/oauth/odoo-callback"]
    assert len(query["state"]) == 1  # our own txn_id, not the client's state


def test_authorize_missing_resource_raises(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    with pytest.raises(AuthorizeError):
        asyncio.run(provider.authorize(_client(), _auth_params(resource=None)))


def test_authorize_unknown_server_raises(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    with pytest.raises(AuthorizeError):
        asyncio.run(
            provider.authorize(
                _client(), _auth_params(resource="https://gw.example.com/mcp/odoo_z")
            )
        )


def test_register_client_persists(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    client = _client()

    asyncio.run(provider.register_client(client))

    loaded = asyncio.run(provider.get_client("client-1"))
    assert loaded is not None
    assert loaded.client_id == "client-1"


def test_register_client_rejects_disallowed_redirect_host(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    client = OAuthClientInformationFull(
        client_id="client-2",
        redirect_uris=["https://evil.example.com/cb"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )

    with pytest.raises(RegistrationError):
        asyncio.run(provider.register_client(client))


def test_complete_odoo_login_unknown_txn_raises_lookup_error(tmp_path: Path) -> None:
    provider = _provider(tmp_path)

    with pytest.raises(LookupError):
        asyncio.run(provider.complete_odoo_login("does-not-exist", code=None, error=None))


def test_complete_odoo_login_denied_redirects_with_error(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    redirect_url = asyncio.run(provider.authorize(_client(), _auth_params()))
    txn_id = parse_qs(urlparse(redirect_url).query)["state"][0]

    result = asyncio.run(
        provider.complete_odoo_login(txn_id, code=None, error="access_denied")
    )

    parsed = parse_qs(urlparse(result).query)
    assert parsed["error"] == ["access_denied"]
    assert parsed["state"] == ["state-1"]  # the *client's* original state


def test_complete_odoo_login_same_txn_cannot_be_reused(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    redirect_url = asyncio.run(provider.authorize(_client(), _auth_params()))
    txn_id = parse_qs(urlparse(redirect_url).query)["state"][0]

    asyncio.run(provider.complete_odoo_login(txn_id, code=None, error="access_denied"))

    with pytest.raises(LookupError):
        asyncio.run(provider.complete_odoo_login(txn_id, code=None, error="access_denied"))


def test_redeem_with_odoo_sends_x_odoo_database_header_when_db_set(tmp_path: Path) -> None:
    """A server-to-server call carries no Odoo session cookie - on a host
    serving more than one database with no `dbfilter` narrowing it to a
    single one, Odoo can't otherwise tell which database to route the
    request to and 404s before the addon's controller ever runs. The
    `X-Odoo-Database` header (same mechanism `Json2Backend` already sends
    for `/json/2/...`) makes that stateless."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"result": {"api_key": "K", "login": "a", "uid": 1}})

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "odoo_mcp.auth.provider.httpx.post",
        lambda url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).post(url, **kw),
    )
    try:
        provider = _provider(tmp_path)
        server = ServerConfig(
            id="odoo_a", url="https://odoo-a.example.com", db="hrm19.test.062026", version=19, backend="json2"
        )
        result = asyncio.run(
            provider._redeem_with_odoo(server, code="odoo-code", channel_secret="channel-secret")
        )
        assert result == {"api_key": "K", "login": "a", "uid": 1}
        assert captured[0].headers["X-Odoo-Database"] == "hrm19.test.062026"
    finally:
        monkeypatch.undo()


def test_redeem_with_odoo_omits_header_when_db_not_set(tmp_path: Path) -> None:
    """`_server()`'s default (`db=None`, e.g. a single-database server) -
    no reason to send a database hint Odoo never asked for."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"result": {"api_key": "K", "login": "a", "uid": 1}})

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "odoo_mcp.auth.provider.httpx.post",
        lambda url, **kw: httpx.Client(transport=httpx.MockTransport(handler)).post(url, **kw),
    )
    try:
        provider = _provider(tmp_path)
        result = asyncio.run(
            provider._redeem_with_odoo(_server(), code="odoo-code", channel_secret="channel-secret")
        )
        assert result == {"api_key": "K", "login": "a", "uid": 1}
        assert "X-Odoo-Database" not in captured[0].headers
    finally:
        monkeypatch.undo()


def test_complete_odoo_login_success_mints_code_client_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Odoo's own /odoo_mcp/token rejects the code (e.g. bad channel
    secret, expired), the browser still ends up back at Claude's
    redirect_uri with an error - never a broken/blank page."""
    provider = _provider(tmp_path)

    async def _fake_redeem(server, *, code, channel_secret):
        return {"error": "invalid_grant"}

    monkeypatch.setattr(provider, "_redeem_with_odoo", _fake_redeem)

    redirect_url = asyncio.run(provider.authorize(_client(), _auth_params()))
    txn_id = parse_qs(urlparse(redirect_url).query)["state"][0]

    result = asyncio.run(provider.complete_odoo_login(txn_id, code="odoo-code", error=None))

    assert parse_qs(urlparse(result).query)["error"] == ["access_denied"]


def _complete_successful_login(
    provider: OdooMcpAuthProvider, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, OdooAuthorizationCode]:
    async def _fake_redeem(server, *, code, channel_secret):
        assert channel_secret == "channel-secret"
        return {"api_key": "RAW-ODOO-KEY", "login": "alice@example.com", "uid": 7}

    monkeypatch.setattr(provider, "_redeem_with_odoo", _fake_redeem)

    redirect_url = asyncio.run(provider.authorize(_client(), _auth_params()))
    txn_id = parse_qs(urlparse(redirect_url).query)["state"][0]
    final_redirect = asyncio.run(
        provider.complete_odoo_login(txn_id, code="odoo-code", error=None)
    )
    code = parse_qs(urlparse(final_redirect).query)["code"][0]
    auth_code = asyncio.run(provider.load_authorization_code(_client(), code))
    assert auth_code is not None
    return final_redirect, auth_code


def test_complete_odoo_login_success_redirects_with_our_own_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)

    final_redirect, auth_code = _complete_successful_login(provider, monkeypatch)

    query = parse_qs(urlparse(final_redirect).query)
    assert query["state"] == ["state-1"]
    assert auth_code.server_id == "odoo_a"
    assert auth_code.odoo_uid == 7
    assert auth_code.odoo_login == "alice@example.com"
    assert auth_code.api_key == "RAW-ODOO-KEY"


def test_exchange_authorization_code_issues_working_access_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)
    _, auth_code = _complete_successful_login(provider, monkeypatch)

    token = asyncio.run(provider.exchange_authorization_code(_client(), auth_code))

    assert token.refresh_token is not None
    resolved = asyncio.run(provider.load_access_token(token.access_token))
    assert isinstance(resolved, OdooAccessToken)
    assert resolved.server_id == "odoo_a"
    assert resolved.odoo_uid == 7
    assert resolved.api_key == "RAW-ODOO-KEY"

    # single-use: the code cannot be exchanged a second time
    assert asyncio.run(provider.load_authorization_code(_client(), auth_code.code)) is None


def test_refresh_token_rotates_and_invalidates_old_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _provider(tmp_path)
    _, auth_code = _complete_successful_login(provider, monkeypatch)
    token = asyncio.run(provider.exchange_authorization_code(_client(), auth_code))

    loaded_refresh = asyncio.run(provider.load_refresh_token(_client(), token.refresh_token))
    assert loaded_refresh is not None
    new_token = asyncio.run(
        provider.exchange_refresh_token(_client(), loaded_refresh, ["mcp"])
    )

    assert new_token.access_token != token.access_token
    assert new_token.refresh_token != token.refresh_token
    # rotation revokes the old pair entirely (access + refresh share one
    # vault row) - see OdooMcpAuthProvider.exchange_refresh_token
    assert asyncio.run(provider.load_access_token(token.access_token)) is None
    assert asyncio.run(provider.load_refresh_token(_client(), token.refresh_token)) is None
    # the new access token resolves to the same Odoo identity
    resolved = asyncio.run(provider.load_access_token(new_token.access_token))
    assert resolved.odoo_uid == 7
    assert resolved.api_key == "RAW-ODOO-KEY"


def test_exchange_refresh_token_unknown_token_raises_token_error(tmp_path: Path) -> None:
    from mcp.server.auth.provider import RefreshToken

    provider = _provider(tmp_path)
    bogus = RefreshToken(token="no-such-token", client_id="client-1", scopes=["mcp"])

    with pytest.raises(TokenError):
        asyncio.run(provider.exchange_refresh_token(_client(), bogus, ["mcp"]))
