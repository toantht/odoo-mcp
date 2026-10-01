"""Phase 5: server registry loader (`config/servers.yaml`).

Replaces hard-coded `ODOO_URL` / `ODOO_DB` env vars with a small YAML
registry (`url`, `db`, `version`, `backend` per server id - see
`config/servers.example.yaml`), so pointing at a different pilot server
is a config change, not a code change. The API key still comes from the
`ODOO_API_KEY` env var (not the registry) - see Phase 5 notes in the
plan for why (no encryption/multi-tenant secrets yet).

Phase 8 adds an optional `login` field, used only by `backend: xmlrpc`
entries (Odoo <=18): XML-RPC has no bearer-token equivalent to JSON-2's
"key = whole identity", every call needs an explicit db + a logged-in
user (`common.authenticate(db, login, password, {})`), so the username
has to live somewhere. It is not secret (a login/email, not a
password/key), so it lives in `servers.yaml` next to `url`/`db` rather
than in an env var; the password half of that pair is still
`ODOO_API_KEY` (Odoo accepts an API key as the XML-RPC password since
14.0) - see `backends/xmlrpc.py`.

Which server to use is picked at runtime via the `ODOO_MCP_SERVER` env
var (e.g. `ODOO_MCP_SERVER=odoo_a`). `Json2Backend.from_env` uses this
registry when `ODOO_MCP_SERVER` is set, falling back to the old
`ODOO_URL` / `ODOO_DB` env vars otherwise (Phase 2-4 behavior).

Phase 9 adds an optional `write_tools` field: a per-server allowlist of
write tool names (e.g. `write_tools: [create_partner]`) that
`core.mcp_server.build_mcp_server` actually registers for that server.
Omitted/empty means no write tools are exposed at all - write access is
opt-in, matching the plan's default `confirm: false` (preview-only)
stance. See `core.mcp_server.WRITE_TOOL_NAMES` for valid names.

Phase 11 added an optional `allowed_models` field (a per-server,
per-model allowlist of which fields the generic
`describe_model`/`search_read`/`create`/`write` tools may read/write).
Phase 16 removes it again: model/field access is now entirely the
API-key user's own Odoo rights (`ir.model.access`/`ir.rule`/field
`groups`) - a leftover `allowed_models:` key in an existing
`servers.yaml` is simply ignored, not an error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_ENV_SERVER = "ODOO_MCP_SERVER"
_ENV_CONFIG_PATH = "ODOO_MCP_CONFIG"
_DEFAULT_CONFIG_PATH = Path("config/servers.yaml")


class RegistryError(RuntimeError):
    """Registry file missing/invalid, or an unknown server id was requested."""


@dataclass(frozen=True)
class ServerConfig:
    """One `servers.yaml` entry. Never carries the API key."""

    id: str
    url: str
    db: str | None
    version: int
    backend: str
    login: str | None = None
    write_tools: tuple[str, ...] = ()


def load_registry(path: str | Path | None = None) -> dict[str, ServerConfig]:
    """Parse a `servers.yaml` registry file into `{server_id: ServerConfig}`.

    path: defaults to the `ODOO_MCP_CONFIG` env var, else
    `config/servers.yaml` (relative to the current working directory).
    """
    config_path = Path(path or os.environ.get(_ENV_CONFIG_PATH) or _DEFAULT_CONFIG_PATH)
    if not config_path.is_file():
        raise RegistryError(
            f"Registry file not found: {config_path} - copy "
            "config/servers.example.yaml to config/servers.yaml and edit it, "
            "or set ODOO_MCP_CONFIG to point at one."
        )

    raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    servers = raw.get("servers") if isinstance(raw, dict) else None
    if not isinstance(servers, dict) or not servers:
        raise RegistryError(f"No servers defined under 'servers:' in {config_path}.")

    registry: dict[str, ServerConfig] = {}
    for server_id, fields in servers.items():
        fields = fields or {}
        try:
            url = fields["url"]
            version = fields["version"]
            backend = fields["backend"]
        except KeyError as exc:
            raise RegistryError(
                f"Server '{server_id}' in {config_path} is missing required field {exc}."
            ) from exc
        registry[server_id] = ServerConfig(
            id=server_id,
            url=str(url),
            db=(str(fields["db"]) if fields.get("db") else None),
            version=int(version),
            backend=str(backend),
            login=(str(fields["login"]) if fields.get("login") else None),
            write_tools=_parse_write_tools(server_id, fields, config_path),
        )
    return registry


def _parse_write_tools(
    server_id: str, fields: dict[str, Any], config_path: Path
) -> tuple[str, ...]:
    """Parse the optional `write_tools:` list. Missing/omitted means no
    write tools (the safe default) - not an error."""
    raw = fields.get("write_tools")
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RegistryError(
            f"Server '{server_id}' in {config_path}: 'write_tools' must be a "
            "list of tool names, e.g. write_tools: [create_partner]."
        )
    return tuple(raw)


def get_server(server_id: str, path: str | Path | None = None) -> ServerConfig:
    """Look up one server by id. Raises `RegistryError` if it's not defined."""
    registry = load_registry(path)
    try:
        return registry[server_id]
    except KeyError:
        available = ", ".join(sorted(registry)) or "(none)"
        raise RegistryError(
            f"Unknown server '{server_id}'. Available: {available}."
        ) from None


def resolve_server(path: str | Path | None = None) -> ServerConfig:
    """Pick the server named by the `ODOO_MCP_SERVER` env var.

    Raises `RegistryError` if the env var is unset/blank or names an
    unknown server.
    """
    server_id = os.environ.get(_ENV_SERVER, "").strip()
    if not server_id:
        raise RegistryError(
            f"{_ENV_SERVER} env var is not set - set it to a server id from "
            "servers.yaml (e.g. ODOO_MCP_SERVER=odoo_a)."
        )
    return get_server(server_id, path)
