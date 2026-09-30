"""Backend implementations, plus small factories that pick the right one.

`Json2Backend` (Phase 2, Odoo 19+) and `XmlRpcBackend` (Phase 8, Odoo
<=18) both implement `core.backend.Backend` identically - tools never
know which one they're calling (see `tests/test_backend_contract.py`).
`backend_from_env` / `build_backend` below are the "which one" decision
point, shared by `gateway/stdio.py` (one backend, from env/registry) and
`gateway/http.py` (one backend per `/mcp/{server_id}`, from an already
-loaded `ServerConfig`).

`write_tools_from_env` (Phase 9) is the analogous decision point for
`gateway/stdio.py`'s write-tool allowlist - `gateway/http.py` doesn't
need it, it already has each server's `ServerConfig.write_tools`
directly.

`allowed_models_from_env` (Phase 11) is the same, one level down: the
per-model read/write field allowlist for `gateway/stdio.py`'s generic
tools (`describe_model`/`search_read`/`create`/`write`). Unlike
`write_tools_from_env`, there is no legacy env-var fallback - the
allowlist is registry-only, so a plain `ODOO_URL` setup (no
`ODOO_MCP_SERVER`) always gets an empty allowlist (every model refused).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from odoo_mcp.config.registry import ModelAccess, RegistryError, ServerConfig, get_server
from odoo_mcp.core.backend import Backend

from .fake import FakeBackend
from .json2 import Json2Backend, Json2Error
from .xmlrpc import XmlRpcBackend, XmlRpcError

__all__ = [
    "FakeBackend",
    "Json2Backend",
    "Json2Error",
    "XmlRpcBackend",
    "XmlRpcError",
    "allowed_models_from_env",
    "backend_from_env",
    "build_backend",
    "write_tools_from_env",
]


def build_backend(config: ServerConfig, *, api_key: str) -> Backend:
    """Build the right `Backend` for one already-resolved `ServerConfig`,
    dispatching on its `backend:` field. Used by `gateway/http.py`,
    which already has the `ServerConfig` and API key - no env/registry
    lookups happen here.

    Raises `Json2Error`/`XmlRpcError` if that server's `backend`-specific
    required fields (e.g. xmlrpc's `db`/`login`) are missing, or
    `ValueError` for an unknown `backend:` value.
    """
    if config.backend == "json2":
        return Json2Backend(config.url, api_key, db=config.db)
    if config.backend == "xmlrpc":
        if not config.db:
            raise XmlRpcError(
                f"Server '{config.id}' is missing 'db' in servers.yaml "
                "(required for the xmlrpc backend)."
            )
        if not config.login:
            raise XmlRpcError(
                f"Server '{config.id}' is missing 'login' in servers.yaml "
                "(required for the xmlrpc backend)."
            )
        return XmlRpcBackend(config.url, config.db, config.login, api_key)
    raise ValueError(f"Server '{config.id}': unsupported backend '{config.backend}'.")


def backend_from_env(*, load_dotenv_file: bool = True) -> Backend:
    """Build whichever single `Backend` `gateway/stdio.py` needs.

    Mirrors `Json2Backend.from_env` (Phase 2-5): if `ODOO_MCP_SERVER` is
    set, looks up that server in the Phase 5 registry and dispatches on
    its `backend:` field (`json2` -> `Json2Backend`, `xmlrpc` (Phase 8)
    -> `XmlRpcBackend`). Falls back to the raw `ODOO_URL`/`ODOO_DB` env
    vars (Phase 2-4 behavior, `Json2Backend` only) when unset.
    """
    if load_dotenv_file:
        load_dotenv()

    server_id = os.environ.get("ODOO_MCP_SERVER", "").strip()
    if not server_id:
        return Json2Backend.from_env(load_dotenv_file=False)

    try:
        server = get_server(server_id)
    except RegistryError as exc:
        raise Json2Error(str(exc)) from exc

    if server.backend == "json2":
        return Json2Backend.from_env(load_dotenv_file=False)
    if server.backend == "xmlrpc":
        return XmlRpcBackend.from_env(load_dotenv_file=False)
    raise Json2Error(
        f"Server '{server_id}' has unsupported backend '{server.backend}'."
    )


def write_tools_from_env(*, load_dotenv_file: bool = True) -> frozenset[str]:
    """Phase 9: write-tool allowlist for `gateway/stdio.py`'s single
    `Backend`, mirroring `backend_from_env`'s registry-or-env fallback.

    If `ODOO_MCP_SERVER` is set, uses that server's `write_tools:` entry
    in the Phase 5 registry (`config/servers.yaml`). Otherwise falls
    back to the comma-separated `ODOO_MCP_WRITE_TOOLS` env var (Phase
    2-4 style, no registry file). Either way, defaults to no write
    tools - write access is opt-in, never on by accident.
    """
    if load_dotenv_file:
        load_dotenv()

    server_id = os.environ.get("ODOO_MCP_SERVER", "").strip()
    if server_id:
        try:
            server = get_server(server_id)
        except RegistryError as exc:
            raise Json2Error(str(exc)) from exc
        return frozenset(server.write_tools)

    raw = os.environ.get("ODOO_MCP_WRITE_TOOLS", "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def allowed_models_from_env(*, load_dotenv_file: bool = True) -> dict[str, ModelAccess]:
    """Phase 11: per-model read/write field allowlist for
    `gateway/stdio.py`'s single `Backend`, mirroring `write_tools_from_env`'s
    registry-based lookup.

    If `ODOO_MCP_SERVER` is set, uses that server's `allowed_models:`
    entry in the Phase 5 registry (`config/servers.yaml`). Otherwise
    (no registry - a raw `ODOO_URL` setup) there is no config source for
    this, so it defaults to empty - no model is opened at all.
    """
    if load_dotenv_file:
        load_dotenv()

    server_id = os.environ.get("ODOO_MCP_SERVER", "").strip()
    if not server_id:
        return {}

    try:
        server = get_server(server_id)
    except RegistryError as exc:
        raise Json2Error(str(exc)) from exc
    return dict(server.allowed_models)
