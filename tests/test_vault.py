"""Phase 12 unit tests: `odoo_mcp.auth.vault.Vault`.

No network, no real Odoo - a throwaway SQLite file per test (`tmp_path`).
Covers what `auth/provider.py` relies on: client registration round-trips,
token store/lookup/rotate, and that a wrong `ODOO_MCP_VAULT_KEY` fails
loudly instead of silently returning garbage.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull

from odoo_mcp.auth.vault import Vault, VaultError


def _client(client_id: str = "client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )


def test_register_and_get_client_round_trips(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault.sqlite3", vault_key="secret-key")

    vault.register_client(_client())
    loaded = vault.get_client("client-1")

    assert loaded is not None
    assert loaded.client_id == "client-1"
    assert str(loaded.redirect_uris[0]) == "https://claude.ai/api/mcp/auth_callback"


def test_get_client_unknown_returns_none(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault.sqlite3", vault_key="secret-key")

    assert vault.get_client("does-not-exist") is None


def test_store_and_lookup_access_token_decrypts_api_key(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault.sqlite3", vault_key="secret-key")

    vault.store_token(
        access_token="access-1",
        refresh_token="refresh-1",
        client_id="client-1",
        server_id="odoo_a",
        odoo_uid=7,
        odoo_login="alice@example.com",
        api_key="RAW-ODOO-API-KEY",
        scopes=["mcp"],
        resource="https://gw.example.com/mcp/odoo_a",
        access_ttl_seconds=3600,
        refresh_ttl_seconds=3600 * 24 * 30,
    )

    record = vault.lookup_by_access_token("access-1")
    assert record is not None
    assert record.server_id == "odoo_a"
    assert record.odoo_uid == 7
    assert record.odoo_login == "alice@example.com"
    assert record.api_key == "RAW-ODOO-API-KEY"
    assert record.scopes == ("mcp",)


def test_lookup_by_refresh_token_returns_same_row(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault.sqlite3", vault_key="secret-key")
    vault.store_token(
        access_token="access-1",
        refresh_token="refresh-1",
        client_id="client-1",
        server_id="odoo_a",
        odoo_uid=7,
        odoo_login="alice@example.com",
        api_key="RAW-KEY",
        scopes=["mcp"],
        resource=None,
        access_ttl_seconds=3600,
        refresh_ttl_seconds=3600,
    )

    record = vault.lookup_by_refresh_token("refresh-1")
    assert record is not None
    assert record.api_key == "RAW-KEY"


def test_lookup_missing_token_returns_none(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault.sqlite3", vault_key="secret-key")

    assert vault.lookup_by_access_token("nope") is None
    assert vault.lookup_by_refresh_token("nope") is None


def test_delete_by_access_token_removes_row(tmp_path: Path) -> None:
    vault = Vault(tmp_path / "vault.sqlite3", vault_key="secret-key")
    vault.store_token(
        access_token="access-1",
        refresh_token="refresh-1",
        client_id="client-1",
        server_id="odoo_a",
        odoo_uid=1,
        odoo_login="a",
        api_key="K",
        scopes=["mcp"],
        resource=None,
        access_ttl_seconds=3600,
        refresh_ttl_seconds=3600,
    )

    vault.delete_by_access_token("access-1")

    assert vault.lookup_by_access_token("access-1") is None


def test_store_token_rotation_invalidates_old_refresh_token(tmp_path: Path) -> None:
    """Mirrors what `OdooMcpAuthProvider.exchange_refresh_token` does:
    delete the old row by its refresh hash, then store a fresh pair."""
    vault = Vault(tmp_path / "vault.sqlite3", vault_key="secret-key")
    vault.store_token(
        access_token="access-1",
        refresh_token="refresh-1",
        client_id="client-1",
        server_id="odoo_a",
        odoo_uid=1,
        odoo_login="a",
        api_key="K",
        scopes=["mcp"],
        resource=None,
        access_ttl_seconds=3600,
        refresh_ttl_seconds=3600,
    )

    vault.delete_by_refresh_token("refresh-1")
    vault.store_token(
        access_token="access-2",
        refresh_token="refresh-2",
        client_id="client-1",
        server_id="odoo_a",
        odoo_uid=1,
        odoo_login="a",
        api_key="K",
        scopes=["mcp"],
        resource=None,
        access_ttl_seconds=3600,
        refresh_ttl_seconds=3600,
    )

    assert vault.lookup_by_refresh_token("refresh-1") is None
    assert vault.lookup_by_access_token("access-1") is None
    assert vault.lookup_by_refresh_token("refresh-2") is not None


def test_wrong_vault_key_raises_on_decrypt(tmp_path: Path) -> None:
    path = tmp_path / "vault.sqlite3"
    vault = Vault(path, vault_key="right-key")
    vault.store_token(
        access_token="access-1",
        refresh_token=None,
        client_id="client-1",
        server_id="odoo_a",
        odoo_uid=1,
        odoo_login="a",
        api_key="K",
        scopes=["mcp"],
        resource=None,
        access_ttl_seconds=3600,
        refresh_ttl_seconds=None,
    )

    other_vault = Vault(path, vault_key="wrong-key")
    with pytest.raises(VaultError, match="ODOO_MCP_VAULT_KEY"):
        other_vault.lookup_by_access_token("access-1")


def test_missing_vault_key_raises(tmp_path: Path) -> None:
    with pytest.raises(VaultError, match="ODOO_MCP_VAULT_KEY"):
        Vault(tmp_path / "vault.sqlite3", vault_key="")
