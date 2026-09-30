"""SQLite-backed vault: dynamically-registered OAuth clients (Claude, per
the plan's "một issuer cho mọi connector" - one shared authorization
server, so one client registration is reused across every `/mcp/{id}`
connector the user later authorizes) plus issued access/refresh tokens.

Only the *bearer* MCP tokens are stored here in hashed form (sha256 -
irreversible, matching `gateway/http.py`'s `RateLimitMiddleware` which
already never keeps a raw `Authorization` header either). The Odoo API
key each token maps to is Fernet-encrypted with `ODOO_MCP_VAULT_KEY`
before it ever touches disk - see `_fernet_from_secret` (deliberately the
same derivation as `addon/odoo_mcp/models/auth_code.py`'s helper, but a
different secret/key: the addon's `odoo_mcp.channel_secret` only ever
protects a code that lives a couple of minutes on the Odoo side, while
`ODOO_MCP_VAULT_KEY` protects the long-lived credential this file stores
on the gateway side - the two are never the same secret and never need
to be).
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from mcp.shared.auth import OAuthClientInformationFull


class VaultError(RuntimeError):
    """Missing/invalid `ODOO_MCP_VAULT_KEY`, or a row that fails to
    decrypt with it (wrong key, or tampered/corrupted data)."""


def _fernet_from_secret(secret: str) -> Fernet:
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class TokenRecord:
    """One redeemed access/refresh token pair, as stored - `api_key` is
    already decrypted by the time callers see this (see
    `Vault.lookup_by_access_token` / `lookup_by_refresh_token`)."""

    client_id: str
    server_id: str
    odoo_uid: int
    odoo_login: str
    api_key: str
    scopes: tuple[str, ...]
    resource: str | None
    access_expires_at: float
    refresh_expires_at: float | None


class Vault:
    """One SQLite file (`ODOO_MCP_VAULT_PATH`, default
    `config/vault.sqlite3`, git-ignored like `servers.yaml`) for the whole
    gateway process - safe for a single instance (same assumption
    `gateway/http.py`'s in-memory `RateLimitMiddleware` already makes;
    see its module docstring)."""

    def __init__(self, path: str | Path, *, vault_key: str) -> None:
        if not vault_key:
            raise VaultError("Missing ODOO_MCP_VAULT_KEY env var.")
        self._fernet = _fernet_from_secret(vault_key)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tokens (
                access_hash TEXT PRIMARY KEY,
                refresh_hash TEXT,
                client_id TEXT NOT NULL,
                server_id TEXT NOT NULL,
                odoo_uid INTEGER NOT NULL,
                odoo_login TEXT NOT NULL,
                encrypted_api_key TEXT NOT NULL,
                scopes TEXT NOT NULL,
                resource TEXT,
                access_expires_at REAL NOT NULL,
                refresh_expires_at REAL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS tokens_refresh_hash ON tokens (refresh_hash)"
        )
        self._conn.commit()

    # ---- OAuth clients (RFC 7591 dynamic registration) ----

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = self._conn.execute(
            "SELECT data FROM oauth_clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        if row is None:
            return None
        return OAuthClientInformationFull.model_validate_json(row[0])

    def register_client(self, client: OAuthClientInformationFull) -> None:
        assert client.client_id, "client_id must be assigned before register_client()"
        self._conn.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, data, created_at) VALUES (?, ?, ?)",
            (client.client_id, client.model_dump_json(), time.time()),
        )
        self._conn.commit()

    # ---- Tokens ----

    def store_token(
        self,
        *,
        access_token: str,
        refresh_token: str | None,
        client_id: str,
        server_id: str,
        odoo_uid: int,
        odoo_login: str,
        api_key: str,
        scopes: list[str],
        resource: str | None,
        access_ttl_seconds: float,
        refresh_ttl_seconds: float | None,
    ) -> None:
        now = time.time()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO tokens (
                access_hash, refresh_hash, client_id, server_id, odoo_uid,
                odoo_login, encrypted_api_key, scopes, resource,
                access_expires_at, refresh_expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _hash(access_token),
                _hash(refresh_token) if refresh_token else None,
                client_id,
                server_id,
                odoo_uid,
                odoo_login,
                self._fernet.encrypt(api_key.encode()).decode(),
                json.dumps(scopes),
                resource,
                now + access_ttl_seconds,
                (now + refresh_ttl_seconds) if refresh_ttl_seconds else None,
            ),
        )
        self._conn.commit()

    def lookup_by_access_token(self, token: str) -> TokenRecord | None:
        return self._lookup("access_hash", _hash(token))

    def lookup_by_refresh_token(self, token: str) -> TokenRecord | None:
        return self._lookup("refresh_hash", _hash(token))

    def delete_by_access_token(self, token: str) -> None:
        self._conn.execute("DELETE FROM tokens WHERE access_hash = ?", (_hash(token),))
        self._conn.commit()

    def delete_by_refresh_token(self, token: str) -> None:
        self._conn.execute("DELETE FROM tokens WHERE refresh_hash = ?", (_hash(token),))
        self._conn.commit()

    def _lookup(self, column: str, value: str) -> TokenRecord | None:
        row = self._conn.execute(
            f"""
            SELECT client_id, server_id, odoo_uid, odoo_login, encrypted_api_key,
                   scopes, resource, access_expires_at, refresh_expires_at
            FROM tokens WHERE {column} = ?
            """,
            (value,),
        ).fetchone()
        if row is None:
            return None
        (
            client_id,
            server_id,
            odoo_uid,
            odoo_login,
            encrypted_api_key,
            scopes_json,
            resource,
            access_expires_at,
            refresh_expires_at,
        ) = row
        try:
            api_key = self._fernet.decrypt(encrypted_api_key.encode()).decode()
        except InvalidToken as exc:
            raise VaultError(
                "Stored API key could not be decrypted - ODOO_MCP_VAULT_KEY changed?"
            ) from exc
        return TokenRecord(
            client_id=client_id,
            server_id=server_id,
            odoo_uid=odoo_uid,
            odoo_login=odoo_login,
            api_key=api_key,
            scopes=tuple(json.loads(scopes_json)),
            resource=resource,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
        )
