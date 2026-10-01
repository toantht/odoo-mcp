"""`OAuthAuthorizationServerProvider` for the MCP gateway (Phase 12 - see
blueprint-v3.md and `auth/__init__.py`). Wires the `mcp` SDK's standard
OAuth 2.1 handlers (`mcp.server.auth.routes.create_auth_routes` - PKCE,
dynamic client registration, `/token`) to the Odoo consent flow instead of
a normal login form:

    Claude --/authorize--> this provider --redirect--> Odoo /odoo_mcp/authorize
                                                              |
                                            user logs in (if needed), clicks allow
                                                              |
    this provider <--/oauth/odoo-callback-- Odoo -----redirect + one-time code

`authorize()` below only ever returns a redirect to the *Odoo* server picked
via the `resource` (RFC 8707) query param Claude sends - it never shows a
login form of its own. `complete_odoo_login()` (called from the plain
`/oauth/odoo-callback` route added in `gateway/http.py`, not part of the
SDK's protocol) is the other half: it exchanges the addon's one-time code
for the user's freshly-generated Odoo API key over a direct server-to-
server call, mints *our own* one-time authorization code for it, and sends
the browser back to Claude's real `redirect_uri`.

Everything the SDK protocol itself asks for (`load_authorization_code`,
`exchange_authorization_code`, `load_refresh_token`,
`exchange_refresh_token`, `load_access_token`) after that point is normal
OAuth bookkeeping against `vault.Vault` - see that module for what is
actually persisted (hashed tokens, Fernet-encrypted API key).
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote, urlparse

import httpx
from pydantic import AnyHttpUrl, AnyUrl
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.routing import Route

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
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from odoo_mcp.config.registry import ServerConfig
from .vault import Vault

logger = logging.getLogger("odoo_mcp.auth.provider")

#: Scope every issued token carries - the gateway has exactly one kind of
#: access (call this server's MCP tools as the consenting user), so there
#: is nothing finer-grained to ask for yet.
SCOPE = "mcp"
ACCESS_TOKEN_TTL_SECONDS = 3600
REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30
AUTH_CODE_TTL_SECONDS = 300
#: How long a `/authorize` -> Odoo login/consent round trip may take
#: before `authorize()`'s pending transaction is considered abandoned.
TXN_TTL_SECONDS = 600


class OdooAccessToken(AccessToken):
    """`AccessToken` plus the Odoo identity/key it resolves to. Never
    serialized back to the client (see the SDK's own note on
    `OAuthAuthorizationServerProvider` subclasses) - `core.mcp_server`
    reads `.api_key` straight off this via `get_access_token()`
    (`mcp.server.auth.middleware.auth_context`) inside each tool call."""

    server_id: str
    odoo_uid: int
    odoo_login: str
    api_key: str


class OdooAuthorizationCode(AuthorizationCode):
    """Same idea as `OdooAccessToken`, for the short window between
    `complete_odoo_login()` minting this code and Claude exchanging it at
    `/token` (`exchange_authorization_code` below)."""

    server_id: str
    odoo_uid: int
    odoo_login: str
    api_key: str


@dataclass
class _PendingTxn:
    """One in-flight `/authorize` -> Odoo consent round trip, keyed by a
    random `txn_id` this provider generates (never the client's own
    `state`, which we pass through untouched to the *real* redirect at the
    end). In-memory only, per gateway process - a restart mid-login just
    means the user retries, same as any other OAuth provider outage."""

    client_id: str
    server_id: str
    redirect_uri: AnyUrl
    redirect_uri_provided_explicitly: bool
    state: str | None
    code_challenge: str
    scopes: list[str]
    resource: str | None
    created_at: float


def _server_id_from_resource(resource: str | None) -> str | None:
    """`resource` (RFC 8707) is expected to be this gateway's own
    `.../mcp/<server_id>` URL - see `/.well-known/oauth-protected-resource
    /mcp/<server_id>` (`mcp.server.auth.routes.create_protected_resource_routes`,
    mounted per server in `gateway/http.py`), which is what tells Claude to
    send it in the first place."""
    if not resource:
        return None
    path = urlparse(resource).path.rstrip("/")
    marker = "/mcp/"
    if marker not in path:
        return None
    return path.rsplit(marker, 1)[1] or None


class OdooMcpAuthProvider:
    """One instance for the whole gateway (`gateway/http.py`), shared by
    every mounted `/mcp/{server_id}` - see module docstring."""

    def __init__(
        self,
        registry: dict[str, ServerConfig],
        vault: Vault,
        *,
        public_url: str,
        allowed_redirect_hosts: set[str],
        channel_secret_for_server: Callable[[str], str],
    ) -> None:
        self._registry = registry
        self._vault = vault
        self._public_url = public_url.rstrip("/")
        self._allowed_hosts = allowed_redirect_hosts
        self._channel_secret_for_server = channel_secret_for_server
        self._pending_txns: dict[str, _PendingTxn] = {}
        self._auth_codes: dict[str, OdooAuthorizationCode] = {}

    @property
    def callback_url(self) -> str:
        return f"{self._public_url}/oauth/odoo-callback"

    # ---- OAuthAuthorizationServerProvider protocol ----

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._vault.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            host = urlparse(str(uri)).hostname or ""
            if host not in self._allowed_hosts:
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=(
                        f"redirect_uri host '{host}' is not on this gateway's "
                        "allowlist (ODOO_MCP_OAUTH_REDIRECT_HOSTS)."
                    ),
                )
        self._vault.register_client(client_info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        server_id = _server_id_from_resource(params.resource)
        if server_id is None or server_id not in self._registry:
            raise AuthorizeError(
                error="invalid_request",
                error_description=(
                    "Missing or unknown 'resource' - expected this gateway's "
                    "own '<public_url>/mcp/<server_id>'."
                ),
            )
        self._prune_pending()
        txn_id = secrets.token_urlsafe(24)
        self._pending_txns[txn_id] = _PendingTxn(
            client_id=client.client_id or "",
            server_id=server_id,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            state=params.state,
            code_challenge=params.code_challenge,
            scopes=params.scopes or [SCOPE],
            resource=params.resource,
            created_at=time.time(),
        )
        server = self._registry[server_id]
        return (
            f"{server.url.rstrip('/')}/odoo_mcp/authorize"
            f"?redirect_uri={quote(self.callback_url, safe='')}"
            f"&state={quote(txn_id, safe='')}"
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> OdooAuthorizationCode | None:
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: OdooAuthorizationCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        self._vault.store_token(
            access_token=access_token,
            refresh_token=refresh_token,
            client_id=authorization_code.client_id,
            server_id=authorization_code.server_id,
            odoo_uid=authorization_code.odoo_uid,
            odoo_login=authorization_code.odoo_login,
            api_key=authorization_code.api_key,
            scopes=authorization_code.scopes,
            resource=authorization_code.resource,
            access_ttl_seconds=ACCESS_TOKEN_TTL_SECONDS,
            refresh_ttl_seconds=REFRESH_TOKEN_TTL_SECONDS,
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = self._vault.lookup_by_refresh_token(refresh_token)
        if record is None or record.client_id != (client.client_id or ""):
            return None
        if record.refresh_expires_at and record.refresh_expires_at < time.time():
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record.client_id,
            scopes=list(record.scopes),
            expires_at=int(record.refresh_expires_at) if record.refresh_expires_at else None,
            resource=record.resource,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        record = self._vault.lookup_by_refresh_token(refresh_token.token)
        if record is None:
            raise TokenError(error="invalid_grant", error_description="refresh token not found.")
        # Rotate both tokens (RFC 6749 best practice) - the Odoo API key
        # itself is unaffected, only its MCP-side wrapper changes.
        self._vault.delete_by_refresh_token(refresh_token.token)
        new_access = secrets.token_urlsafe(32)
        new_refresh = secrets.token_urlsafe(32)
        self._vault.store_token(
            access_token=new_access,
            refresh_token=new_refresh,
            client_id=record.client_id,
            server_id=record.server_id,
            odoo_uid=record.odoo_uid,
            odoo_login=record.odoo_login,
            api_key=record.api_key,
            scopes=list(scopes or record.scopes),
            resource=record.resource,
            access_ttl_seconds=ACCESS_TOKEN_TTL_SECONDS,
            refresh_ttl_seconds=REFRESH_TOKEN_TTL_SECONDS,
        )
        return OAuthToken(
            access_token=new_access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL_SECONDS,
            refresh_token=new_refresh,
            scope=" ".join(scopes or record.scopes),
        )

    async def load_access_token(self, token: str) -> OdooAccessToken | None:
        record = self._vault.lookup_by_access_token(token)
        if record is None:
            return None
        if record.access_expires_at < time.time():
            self._vault.delete_by_access_token(token)
            return None
        return OdooAccessToken(
            token=token,
            client_id=record.client_id,
            scopes=list(record.scopes),
            expires_at=int(record.access_expires_at),
            resource=record.resource,
            subject=f"{record.server_id}:{record.odoo_uid}",
            server_id=record.server_id,
            odoo_uid=record.odoo_uid,
            odoo_login=record.odoo_login,
            api_key=record.api_key,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            self._vault.delete_by_access_token(token.token)
        else:
            self._vault.delete_by_refresh_token(token.token)

    # ---- Odoo consent callback (not part of the SDK protocol - a plain
    # route in gateway/http.py calls this directly) ----

    async def complete_odoo_login(
        self, txn_id: str, *, code: str | None, error: str | None
    ) -> str:
        """Second half of `authorize()`: the Odoo addon redirected the
        browser to `/oauth/odoo-callback` (`callback_url` above) with
        either `code` (user clicked allow) or `error` (denied/misconfigured).
        Either way, resolves to the URL to redirect the browser to next -
        Claude's *real* `redirect_uri`, per RFC 6749 - never raises for a
        bad `code`/`error`; only `LookupError` for an unknown/expired
        `txn_id` (nothing sane to redirect to in that case)."""
        txn = self._pending_txns.pop(txn_id, None)
        if txn is None:
            raise LookupError("Unknown or expired login attempt - please retry from Claude.")

        if error or not code:
            return construct_redirect_uri(
                str(txn.redirect_uri), error=error or "access_denied", state=txn.state
            )

        server = self._registry[txn.server_id]
        channel_secret = self._channel_secret_for_server(txn.server_id)
        if not channel_secret:
            logger.error(
                "No channel secret configured for server '%s' "
                "(ODOO_MCP_CHANNEL_SECRET_%s) - cannot redeem consent code.",
                txn.server_id,
                txn.server_id.upper(),
            )
            return construct_redirect_uri(
                str(txn.redirect_uri), error="server_error", state=txn.state
            )

        payload = await self._redeem_with_odoo(server, code=code, channel_secret=channel_secret)
        if payload is None or "error" in payload:
            logger.warning(
                "odoo_mcp consent redeem for '%s' denied - Odoo returned: %s",
                txn.server_id,
                payload,
            )
            return construct_redirect_uri(
                str(txn.redirect_uri), error="access_denied", state=txn.state
            )

        self._prune_auth_codes()
        auth_code = secrets.token_urlsafe(32)
        self._auth_codes[auth_code] = OdooAuthorizationCode(
            code=auth_code,
            scopes=txn.scopes,
            expires_at=time.time() + AUTH_CODE_TTL_SECONDS,
            client_id=txn.client_id,
            code_challenge=txn.code_challenge,
            redirect_uri=txn.redirect_uri,
            redirect_uri_provided_explicitly=txn.redirect_uri_provided_explicitly,
            resource=txn.resource,
            subject=f"{txn.server_id}:{payload['uid']}",
            server_id=txn.server_id,
            odoo_uid=payload["uid"],
            odoo_login=payload["login"],
            api_key=payload["api_key"],
        )
        return construct_redirect_uri(str(txn.redirect_uri), code=auth_code, state=txn.state)

    async def _redeem_with_odoo(
        self, server: ServerConfig, *, code: str, channel_secret: str
    ) -> dict | None:
        """POSTs to the addon's `/odoo_mcp/token` (`type='jsonrpc'` - see
        `addon/odoo_mcp/controllers/main.py`), which wraps request/response
        in a JSON-RPC 2 envelope (Odoo core behaviour for that route type,
        distinct from the plain-JSON External API `/json/2/...` `Json2Backend`
        calls). Returns the inner `result` dict (`{api_key, login, uid}` or
        `{"error": ...}`), or `None` on any network/HTTP/envelope failure.

        This call carries no Odoo session cookie (it's server-to-server,
        identified only by the one-time `code` + `channel_secret`) - on a
        host serving more than one database with no `dbfilter` narrowing
        it to a single one, Odoo's core dispatch can't otherwise resolve
        *which* database to route the request to and 404s before the
        addon's controller ever runs. The `X-Odoo-Database` header (same
        mechanism `Json2Backend` already sends for `/json/2/...`, see
        `backends/json2.py`) makes that stateless, same as `db_filter`
        would from a hostname - only sent when `server.db` is set, same
        as `Json2Backend`."""
        headers = {"X-Odoo-Database": server.db} if server.db else None
        try:
            response = httpx.post(
                f"{server.url.rstrip('/')}/odoo_mcp/token",
                json={
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "code": code,
                        "redirect_uri": self.callback_url,
                        "secret": channel_secret,
                    },
                    "id": None,
                },
                headers=headers,
                timeout=15.0,
            )
            response.raise_for_status()
            envelope = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("odoo_mcp token exchange with '%s' failed: %s", server.id, exc)
            return None
        if "error" in envelope:
            logger.warning(
                "odoo_mcp token exchange with '%s' returned a JSON-RPC error: %s",
                server.id,
                envelope["error"],
            )
            return None
        return envelope.get("result") or {}

    def _prune_pending(self) -> None:
        cutoff = time.time() - TXN_TTL_SECONDS
        for txn_id, txn in list(self._pending_txns.items()):
            if txn.created_at < cutoff:
                self._pending_txns.pop(txn_id, None)

    def _prune_auth_codes(self) -> None:
        now = time.time()
        for code, auth_code in list(self._auth_codes.items()):
            if auth_code.expires_at < now:
                self._auth_codes.pop(code, None)


def build_routes(
    provider: OdooMcpAuthProvider,
    *,
    issuer_url: AnyHttpUrl,
    client_registration_options: ClientRegistrationOptions,
) -> list[Route]:
    """The shared, gateway-wide OAuth routes: the SDK's own
    `/authorize`, `/token`, `/register`,
    `/.well-known/oauth-authorization-server` (`create_auth_routes` -
    PKCE + dynamic client registration handled entirely by the SDK, see
    `mcp.server.auth.handlers.*`), plus `/oauth/odoo-callback`
    (`OdooMcpAuthProvider.complete_odoo_login` - not part of the SDK
    protocol, this is the second leg of `authorize()`'s redirect to
    Odoo). Mounted once in `gateway/http.py`; per-server protected-
    resource metadata and the `/mcp/{server_id}` bearer check are
    separate, one per server (`create_protected_resource_routes`).
    """
    routes = create_auth_routes(
        provider,
        issuer_url=issuer_url,
        client_registration_options=client_registration_options,
    )

    async def odoo_callback(request: Request) -> RedirectResponse | PlainTextResponse:
        try:
            redirect_url = await provider.complete_odoo_login(
                request.query_params.get("state", ""),
                code=request.query_params.get("code"),
                error=request.query_params.get("error"),
            )
        except LookupError as exc:
            return PlainTextResponse(str(exc), status_code=400)
        return RedirectResponse(
            redirect_url, status_code=302, headers={"Cache-Control": "no-store"}
        )

    routes.append(Route("/oauth/odoo-callback", endpoint=odoo_callback, methods=["GET"]))
    return routes
