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

Phase 11 adds an optional `allowed_models` field: a per-server,
per-model allowlist of which fields the generic
`describe_model`/`search_read`/`create`/`write` tools may read/write,
e.g.:

    allowed_models:
      res.partner:
        read: all                     # or a list of field names
        write: [name, email, phone]   # missing/empty = no write access

Missing/empty (`{}`) means no model is opened at all - same opt-in
stance as `write_tools`. A model not listed here is refused by every
generic tool, including `res.users`/`ir.*` - there is no hard-coded
denylist layered on top of this allowlist.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_SERVER = "ODOO_MCP_SERVER"
_ENV_CONFIG_PATH = "ODOO_MCP_CONFIG"
_DEFAULT_CONFIG_PATH = Path("config/servers.yaml")


class RegistryError(RuntimeError):
    """Registry file missing/invalid, or an unknown server id was requested."""


@dataclass(frozen=True)
class ModelAccess:
    """One `allowed_models.<model>` entry: which fields the generic
    tools may read/write for that model.

    `read` is either the literal string `"all"` (every field returned
    by `fields_get`/`search_read`) or a tuple of allowed field names.
    `write` is a tuple of allowed field names; empty means no write
    access even if a write tool is otherwise enabled via `write_tools`.
    """

    read: str | tuple[str, ...]
    write: tuple[str, ...] = ()


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
    allowed_models: dict[str, ModelAccess] = field(default_factory=dict)


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
            allowed_models=_parse_allowed_models(server_id, fields, config_path),
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


def _parse_allowed_models(
    server_id: str, fields: dict[str, Any], config_path: Path
) -> dict[str, ModelAccess]:
    """Parse the optional `allowed_models:` mapping. Missing/empty means
    no model is opened (the safe default) - not an error."""
    raw = fields.get("allowed_models")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RegistryError(
            f"Server '{server_id}' in {config_path}: 'allowed_models' must be a "
            "mapping of model name to {read, write}, e.g. "
            "allowed_models: {res.partner: {read: all}}."
        )

    result: dict[str, ModelAccess] = {}
    for model_name, spec in raw.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            raise RegistryError(
                f"Server '{server_id}' in {config_path}: allowed_models.'{model_name}' "
                "must be a mapping with a 'read' key, e.g. {read: all}."
            )

        read_raw = spec.get("read")
        if read_raw == "all":
            read: str | tuple[str, ...] = "all"
        elif isinstance(read_raw, list) and all(isinstance(item, str) for item in read_raw):
            read = tuple(read_raw)
        else:
            raise RegistryError(
                f"Server '{server_id}' in {config_path}: allowed_models.'{model_name}'.read "
                "must be 'all' or a list of field names."
            )

        write_raw = spec.get("write")
        if write_raw is None:
            write: tuple[str, ...] = ()
        elif isinstance(write_raw, list) and all(isinstance(item, str) for item in write_raw):
            write = tuple(write_raw)
        else:
            raise RegistryError(
                f"Server '{server_id}' in {config_path}: allowed_models.'{model_name}'.write "
                "must be a list of field names, e.g. write: [name, email]."
            )

        result[model_name] = ModelAccess(read=read, write=write)
    return result


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
