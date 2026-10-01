"""Phase 5 unit tests: `odoo_mcp.config.registry` loader.

Exercises the example registry shipped in the repo
(`config/servers.example.yaml`) plus error paths (missing file, unknown
server id, missing env var) - no network, no real Odoo. Phase 16
removed the `allowed_models` field/model allowlist from `ServerConfig`
entirely - a leftover `allowed_models:` key in an existing file is
simply ignored, not an error (see
`test_leftover_allowed_models_key_is_ignored`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from odoo_mcp.config.registry import (
    RegistryError,
    get_server,
    load_registry,
    resolve_server,
)

_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "config" / "servers.example.yaml"


def test_load_registry_parses_example_file() -> None:
    registry = load_registry(_EXAMPLE_PATH)

    assert set(registry) == {"odoo_a", "odoo_b"}
    server = registry["odoo_a"]
    assert server.id == "odoo_a"
    assert server.url == "https://example.odoo.com"
    assert server.db == "prod"
    assert server.version == 19
    assert server.backend == "json2"
    assert server.login is None


def test_load_registry_parses_xmlrpc_entry_with_login() -> None:
    registry = load_registry(_EXAMPLE_PATH)

    server = registry["odoo_b"]
    assert server.id == "odoo_b"
    assert server.db == "legacy_prod"
    assert server.version == 17
    assert server.backend == "xmlrpc"
    assert server.login == "mcp-integration@example.com"


def test_get_server_returns_matching_entry() -> None:
    server = get_server("odoo_a", _EXAMPLE_PATH)
    assert server.url == "https://example.odoo.com"


def test_get_server_unknown_id_raises() -> None:
    with pytest.raises(RegistryError, match="Unknown server 'odoo_z'"):
        get_server("odoo_z", _EXAMPLE_PATH)


def test_load_registry_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="Registry file not found"):
        load_registry(tmp_path / "does_not_exist.yaml")


def test_load_registry_rejects_empty_servers(tmp_path: Path) -> None:
    config = tmp_path / "servers.yaml"
    config.write_text("servers: {}\n", encoding="utf-8")

    with pytest.raises(RegistryError, match="No servers defined"):
        load_registry(config)


def test_load_registry_rejects_missing_required_field(tmp_path: Path) -> None:
    config = tmp_path / "servers.yaml"
    config.write_text(
        "servers:\n  odoo_a:\n    url: https://example.odoo.com\n    version: 19\n",
        encoding="utf-8",
    )

    with pytest.raises(RegistryError, match="missing required field"):
        load_registry(config)


def test_resolve_server_uses_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_MCP_SERVER", "odoo_a")
    server = resolve_server(_EXAMPLE_PATH)
    assert server.id == "odoo_a"


def test_resolve_server_missing_env_var_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_SERVER", raising=False)
    with pytest.raises(RegistryError, match="ODOO_MCP_SERVER"):
        resolve_server(_EXAMPLE_PATH)


def test_db_optional_when_omitted(tmp_path: Path) -> None:
    config = tmp_path / "servers.yaml"
    config.write_text(
        "servers:\n  single_db:\n    url: https://s.example.com\n"
        "    version: 19\n    backend: json2\n",
        encoding="utf-8",
    )

    registry = load_registry(config)
    assert registry["single_db"].db is None


def test_login_optional_when_omitted(tmp_path: Path) -> None:
    config = tmp_path / "servers.yaml"
    config.write_text(
        "servers:\n  odoo_a:\n    url: https://s.example.com\n"
        "    version: 19\n    backend: json2\n",
        encoding="utf-8",
    )

    registry = load_registry(config)
    assert registry["odoo_a"].login is None


def test_write_tools_defaults_to_empty_when_omitted() -> None:
    registry = load_registry(_EXAMPLE_PATH)

    assert registry["odoo_a"].write_tools == ()
    assert registry["odoo_b"].write_tools == ()


def test_write_tools_parses_list(tmp_path: Path) -> None:
    config = tmp_path / "servers.yaml"
    config.write_text(
        "servers:\n  odoo_a:\n    url: https://s.example.com\n"
        "    version: 19\n    backend: json2\n"
        "    write_tools: [create_partner]\n",
        encoding="utf-8",
    )

    registry = load_registry(config)
    assert registry["odoo_a"].write_tools == ("create_partner",)


def test_write_tools_rejects_non_list(tmp_path: Path) -> None:
    config = tmp_path / "servers.yaml"
    config.write_text(
        "servers:\n  odoo_a:\n    url: https://s.example.com\n"
        "    version: 19\n    backend: json2\n"
        "    write_tools: create_partner\n",
        encoding="utf-8",
    )

    with pytest.raises(RegistryError, match="write_tools"):
        load_registry(config)


def test_leftover_allowed_models_key_is_ignored(tmp_path: Path) -> None:
    """Phase 16 removed the `allowed_models` allowlist - a leftover key
    in an existing `servers.yaml` must not break loading."""
    config = tmp_path / "servers.yaml"
    config.write_text(
        "servers:\n  odoo_a:\n    url: https://s.example.com\n"
        "    version: 19\n    backend: json2\n"
        "    allowed_models:\n"
        "      res.partner:\n"
        "        read: all\n",
        encoding="utf-8",
    )

    registry = load_registry(config)
    assert registry["odoo_a"].id == "odoo_a"
    assert not hasattr(registry["odoo_a"], "allowed_models")
