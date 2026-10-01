"""Shared MCP tool wiring: build a `FastMCP` bound to one `Backend`.

Both `gateway/stdio.py` (Phase 3-5, one server picked via env/registry)
and `gateway/http.py` (Phase 6, N servers, one `FastMCP` per
`/mcp/{server_id}` endpoint) register the exact same tools against the
exact same `Backend` contract - only *how* the `Backend` gets built
differs per gateway. Factoring it out here means adding a tool once
covers both transports.

Phase 11 replaces the fixed, business-shaped tools from Phase 4/9
(`search_partners`, `get_partner`, `create_partner`) with generic,
model-agnostic ones: `list_models`, `describe_model`, `search_read`,
`create`, `write`. An AI caller discovers a model's shape via
`describe_model`/`fields_get` instead of a hard-coded tool per model.
The old fixed-tool functions still live in `core.tools` (used directly
by `scripts/smoke_tool.py`), they are just no longer registered here.

Phase 16 removes the `allowed_models` config allowlist (and the
`list_models` tool that only ever read it): model/field access is now
entirely the API-key user's own Odoo rights (`ir.model.access`,
`ir.rule`, field `groups`) - every generic tool below just forwards the
caller's `model`/`domain`/`fields`/`values` straight to the `Backend`,
and an access violation surfaces as a normal Odoo `AccessError`-derived
tool error, never a local `ValueError` from a config lookup. There is
no way to *discover* "which models can I read" without a model already
in mind - `check_access`/`has_access` are `@api.private` and cannot be
called remotely (see `odoo/orm/models.py`), so that filter cannot live
on the gateway side either.

Safety now rests on one allowlist, read from config, never from the
AI's own request:

- `write_tools` (Phase 9): *which* write tools exist at all on this
  server (`WRITE_TOOL_NAMES` below) - a server not opted in never even
  advertises `create`/`write` via `tools/list`. `confirm` (per call)
  still separately gates whether `create`/`write` actually reach Odoo
  versus just returning a preview.

Phase 12 adds `cache_backend` (default `True`, unchanged behaviour):
`gateway/stdio.py` and any server still on the Phase 5-11 shared-
`ODOO_API_KEY` path (`ODOO_MCP_ALLOW_SHARED_KEY=1`, see `gateway/http.py`)
keep memoizing one `Backend` for the process's lifetime. An OAuth-enabled
HTTP server (the default from Phase 12 on) passes `cache_backend=False`
instead: the API key differs per MCP bearer token/Odoo user (see
`auth/provider.py`'s `OdooAccessToken`), so `backend_factory` must run
again on *every* tool call, reading whichever key the current request's
token resolves to - never the previous caller's.
"""

from __future__ import annotations

from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from odoo_mcp.core.backend import Backend

#: Every write tool `build_mcp_server` knows how to register, keyed by
#: the name used in `write_tools` allowlists (servers.yaml /
#: `ODOO_MCP_WRITE_TOOLS`). Kept as a single source of truth so an
#: unknown name in a config is a clear error, not a silent no-op.
WRITE_TOOL_NAMES: frozenset[str] = frozenset({"create", "write"})

#: `fields_get` attributes `describe_model` returns - just enough for an
#: AI caller to know a field's type/label/relation before calling
#: `search_read`/`create`/`write`, not Odoo's full (much larger) field
#: metadata.
_DESCRIBE_ATTRIBUTES: list[str] = ["string", "type", "required", "relation", "selection"]

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MAX_WRITE_IDS = 100


def build_mcp_server(
    name: str,
    backend_factory: Callable[[], Backend],
    *,
    write_tools: frozenset[str] | set[str] | tuple[str, ...] | None = None,
    cache_backend: bool = True,
    **fastmcp_kwargs: Any,
) -> FastMCP:
    """Create a `FastMCP` exposing `ping` / `describe_model` /
    `search_read` / `search_count` / `read_group` / `name_search`, plus
    any write tool named in `write_tools` (`create`, `write` - Phase
    9, see module docs).

    `backend_factory` is called lazily, the first time a tool call
    actually needs a `Backend` - `ping` and `tools/list` never touch
    it, so a server with a missing/bad config (env var, registry
    entry, ...) still starts and advertises its tools; the config error
    only surfaces as a normal tool error when a real tool is called.

    `cache_backend=True` (default) memoizes that first `Backend` for the
    life of this `FastMCP` - correct when one process/connector means one
    fixed API key (Phase 2-11). Pass `cache_backend=False` (Phase 12's
    OAuth-enabled HTTP servers - see module docs) when `backend_factory`
    itself depends on per-request state (the caller's resolved Odoo API
    key) and must therefore run again on every call.

    `write_tools` defaults to none (no write tools registered at all -
    write access is opt-in). Raises `ValueError` if it names anything
    outside `WRITE_TOOL_NAMES`, so a typo in `servers.yaml` fails loudly
    at startup instead of silently granting/denying the wrong tool.

    Model/field access is not checked here at all (Phase 16) - every
    tool below calls Odoo as the API-key user and lets Odoo's own
    `ir.model.access`/`ir.rule`/field `groups` decide; see module docs.
    """
    allowed_write_tools = frozenset(write_tools or ())
    unknown = allowed_write_tools - WRITE_TOOL_NAMES
    if unknown:
        raise ValueError(
            f"Unknown write tool(s) in allowlist: {sorted(unknown)} - known "
            f"write tools: {sorted(WRITE_TOOL_NAMES)}."
        )

    mcp = FastMCP(name, **fastmcp_kwargs)
    _backend: list[Backend] = []

    def _get_backend() -> Backend:
        if not cache_backend:
            return backend_factory()
        if not _backend:
            _backend.append(backend_factory())
        return _backend[0]

    @mcp.tool()
    def ping() -> str:
        """Health check - confirms the MCP transport is alive. Always
        returns "pong"; does not touch Odoo or any `Backend`."""
        return "pong"

    @mcp.tool()
    def describe_model(
        model: str, fields: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Field metadata (string/type/required/relation/selection) for a
        model. Call before search_read/create/write only when the field
        names are not already known.

        fields: narrow the result to just these field names (default:
        every field). Raises if none of them exist on the model.
        """
        result = _get_backend().fields_get(model, attributes=_DESCRIBE_ATTRIBUTES)
        if not fields:
            return result
        filtered = {name: result[name] for name in fields if name in result}
        if not filtered:
            raise ValueError(f"None of {fields!r} are fields of '{model}'.")
        return filtered

    @mcp.tool()
    def search_read(
        model: str,
        domain: list[Any] | None = None,
        fields: list[str] | None = None,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search + read any Odoo model - returns matching rows. For a
        total or breakdown instead of rows, use search_count/read_group.

        fields: required (no default-all, to avoid pulling binary
        fields like image_1920 by accident). limit: capped at 100.
        """
        if not fields:
            raise ValueError(
                "'fields' must be passed explicitly (e.g. to avoid pulling "
                "large binary fields like image_1920)."
            )
        domain = domain or []
        limit = min(limit, _MAX_LIMIT)
        return _get_backend().search_read(
            model, domain=domain, fields=fields, limit=limit, offset=offset
        )

    @mcp.tool()
    def search_count(model: str, domain: list[Any] | None = None) -> int:
        """Count matching records - returns one integer, not rows. Use
        for "how many" instead of paging search_read and counting."""
        domain = domain or []
        return _get_backend().search_count(model, domain=domain)

    @mcp.tool()
    def read_group(
        model: str,
        fields: list[str],
        groupby: list[str],
        domain: list[Any] | None = None,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Group + aggregate - one row per group, not per record. Use
        instead of search_read-ing every row and totaling yourself.

        fields: Odoo aggregate syntax, e.g. "amount_total:sum". groupby:
        e.g. "date_order:month". limit: caps the number of groups, not
        underlying records; capped at 100.
        """
        domain = domain or []
        limit = min(limit, _MAX_LIMIT)
        return _get_backend().read_group(
            model, domain=domain, fields=fields, groupby=groupby, limit=limit, offset=offset
        )

    @mcp.tool()
    def name_search(
        model: str,
        name: str = "",
        domain: list[Any] | None = None,
        operator: str = "ilike",
        limit: int = _DEFAULT_LIMIT,
    ) -> list[tuple[int, str]]:
        """Resolve a name substring to (id, display_name) pairs. Use to
        find a record's id instead of a full search_read. limit: capped
        at 100."""
        domain = domain or []
        limit = min(limit, _MAX_LIMIT)
        return _get_backend().name_search(
            model, name=name, domain=domain, operator=operator, limit=limit
        )

    if "create" in allowed_write_tools:

        @mcp.tool()
        def create(model: str, values: dict[str, Any], confirm: bool = False) -> dict[str, Any]:
            """Create one record. confirm defaults to false: returns a
            preview only, does not write. Pass confirm=true to actually
            create."""
            if not confirm:
                return {"confirmed": False, "created": False, "preview": values}
            new_id = _get_backend().create(model, values)
            return {"confirmed": True, "created": True, "id": new_id, **values}

    if "write" in allowed_write_tools:

        @mcp.tool()
        def write(
            model: str, ids: list[int], values: dict[str, Any], confirm: bool = False
        ) -> dict[str, Any]:
            """Update existing record(s). ids: non-empty, at most 100.
            confirm defaults to false: returns a preview only, does not
            write. Pass confirm=true to actually update."""
            if not ids:
                raise ValueError("'ids' must not be empty.")
            if len(ids) > _MAX_WRITE_IDS:
                raise ValueError(f"'ids' must not contain more than {_MAX_WRITE_IDS} ids.")
            if not confirm:
                return {"confirmed": False, "written": False, "ids": ids, "preview": values}
            ok = _get_backend().write(model, ids, values)
            return {"confirmed": True, "written": ok, "ids": ids, **values}

    return mcp
