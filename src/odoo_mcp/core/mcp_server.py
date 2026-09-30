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

Safety still rests on two allowlists, both read from config, never
from the AI's own request:

- `write_tools` (Phase 9): *which* write tools exist at all on this
  server (`WRITE_TOOL_NAMES` below) - a server not opted in never even
  advertises `create`/`write` via `tools/list`.
- `allowed_models` (Phase 11, `config.registry.ModelAccess`): *which*
  models, and which fields on each, `describe_model`/`search_read`/
  `create`/`write` may touch. A model missing from this mapping is
  refused by every generic tool, including `res.users`/`ir.*` - there
  is no hard-coded denylist layered on top of it. `confirm` (per call)
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

from odoo_mcp.config.registry import ModelAccess
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


def _require_model_access(allowed_models: dict[str, ModelAccess], model: str) -> ModelAccess:
    access = allowed_models.get(model)
    if access is None:
        raise ValueError(
            f"Model '{model}' is not in this server's allowed_models allowlist "
            "(config/servers.yaml) - every generic tool refuses it, including "
            "system models like res.users/ir.*."
        )
    return access


def _check_read_fields(access: ModelAccess, model: str, fields: list[str]) -> None:
    if access.read == "all":
        return
    allowed = set(access.read)
    denied = sorted(f for f in fields if f not in allowed)
    if denied:
        raise ValueError(
            f"Model '{model}': field(s) {denied} are not in the read allowlist "
            f"{sorted(allowed)}."
        )


def _check_domain_fields(access: ModelAccess, model: str, domain: list[Any]) -> None:
    if access.read == "all":
        return
    allowed = set(access.read)
    for clause in domain:
        if not isinstance(clause, (list, tuple)) or len(clause) != 3:
            continue  # "&"/"|"/"!" logical operators - no field to check
        field = clause[0]
        if isinstance(field, str) and field not in allowed:
            raise ValueError(
                f"Model '{model}': domain field '{field}' is not in the read "
                f"allowlist {sorted(allowed)}."
            )


def _check_groupby_fields(access: ModelAccess, model: str, groupby: list[str]) -> None:
    """Same allowlist check as `_check_domain_fields`, for `read_group`'s
    `groupby` - each entry is a bare field name or `"field:granularity"`
    (dates), so only the part before `:` is checked."""
    if access.read == "all":
        return
    allowed = set(access.read)
    denied = sorted({g.split(":", 1)[0] for g in groupby} - allowed)
    if denied:
        raise ValueError(
            f"Model '{model}': groupby field(s) {denied} are not in the read "
            f"allowlist {sorted(allowed)}."
        )


def _check_aggregate_fields(access: ModelAccess, model: str, fields: list[str]) -> None:
    """Same allowlist check, for `read_group`'s `fields` - each entry is
    a bare field name or `"field:agg"`/`"name:agg(field)"`, so only the
    field name part is checked."""
    if access.read == "all":
        return
    allowed = set(access.read)
    names = set()
    for spec in fields:
        head, _, tail = spec.partition(":")
        names.add(tail.split("(", 1)[1].rstrip(")") if "(" in tail else head)
    denied = sorted(names - allowed)
    if denied:
        raise ValueError(
            f"Model '{model}': field(s) {denied} are not in the read allowlist "
            f"{sorted(allowed)}."
        )


def _check_write_fields(access: ModelAccess, model: str, values: dict[str, Any]) -> None:
    allowed = set(access.write)
    denied = sorted(f for f in values if f not in allowed)
    if denied:
        raise ValueError(
            f"Model '{model}': field(s) {denied} are not in the write allowlist "
            f"{sorted(allowed)} (missing/empty 'write' in allowed_models means no "
            "write access at all, even with a write tool enabled)."
        )


def build_mcp_server(
    name: str,
    backend_factory: Callable[[], Backend],
    *,
    write_tools: frozenset[str] | set[str] | tuple[str, ...] | None = None,
    allowed_models: dict[str, ModelAccess] | None = None,
    cache_backend: bool = True,
    **fastmcp_kwargs: Any,
) -> FastMCP:
    """Create a `FastMCP` exposing `ping` / `list_models` / `describe_model` /
    `search_read` / `search_count` / `read_group` / `name_search`, plus
    any write tool named in `write_tools` (`create`, `write` - Phase
    9/11, see module docs).

    `backend_factory` is called lazily, the first time a tool call
    actually needs a `Backend` - `ping`, `list_models`, and `tools/list`
    never touch it, so a server with a missing/bad config (env var,
    registry entry, ...) still starts and advertises its tools; the
    config error only surfaces as a normal tool error when a real tool
    is called.

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

    `allowed_models` defaults to none (every model refused by every
    generic tool) - see `config.registry.ModelAccess`.
    """
    allowed_write_tools = frozenset(write_tools or ())
    unknown = allowed_write_tools - WRITE_TOOL_NAMES
    if unknown:
        raise ValueError(
            f"Unknown write tool(s) in allowlist: {sorted(unknown)} - known "
            f"write tools: {sorted(WRITE_TOOL_NAMES)}."
        )

    models = dict(allowed_models or {})

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
    def list_models() -> list[str]:
        """List the Odoo models this server's `allowed_models` allowlist
        opens up to `describe_model`/`search_read`/`create`/`write`.
        Reads straight from config - never calls Odoo."""
        return sorted(models)

    @mcp.tool()
    def describe_model(model: str) -> dict[str, dict[str, Any]]:
        """Return field metadata (string/type/required/relation/selection)
        for one allowlisted model, via Odoo's `fields_get` - lets an AI
        caller learn a model's shape before calling `search_read`/
        `create`/`write`.

        Refuses any model not in this server's `allowed_models`
        allowlist. If that model's `read` is a field list (not "all"),
        only those fields are returned.
        """
        access = _require_model_access(models, model)
        fields = _get_backend().fields_get(model, attributes=_DESCRIBE_ATTRIBUTES)
        if access.read == "all":
            return fields
        return {name: attrs for name, attrs in fields.items() if name in access.read}

    @mcp.tool()
    def search_read(
        model: str,
        domain: list[Any] | None = None,
        fields: list[str] | None = None,
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search + read any allowlisted Odoo model in one call - returns
        the matching *rows*. Use this only when the caller actually
        wants to see records.

        For a total, use `search_count` (how many) or `read_group` (a
        sum/count broken down by field) instead - paging this tool just
        to count or total is far more calls and far more tokens for the
        same answer. To resolve a record's id/name (e.g. a many2one
        label), use `name_search` instead of a full row.

        model: must be in this server's `allowed_models` allowlist.
        domain: Odoo domain (list of `[field, operator, value]` clauses);
            every field clause must be in the model's read allowlist
            unless that model's `read` is "all".
        fields: fields to return. If omitted and the model's `read` is a
            field list, that whole list is used. If omitted and `read`
            is "all", this is an error - callers must name fields
            explicitly to avoid pulling large binary fields (e.g.
            `image_1920`).
        limit: max records returned (default 20, capped at 100).
        offset: pagination offset (default 0).
        """
        access = _require_model_access(models, model)
        domain = domain or []
        if fields:
            _check_read_fields(access, model, fields)
        elif access.read == "all":
            raise ValueError(
                f"Model '{model}' allows read: all - 'fields' must be passed "
                "explicitly (e.g. to avoid pulling large binary fields like "
                "image_1920)."
            )
        else:
            fields = list(access.read)
        _check_domain_fields(access, model, domain)
        limit = min(limit, _MAX_LIMIT)
        return _get_backend().search_read(
            model, domain=domain, fields=fields, limit=limit, offset=offset
        )

    @mcp.tool()
    def search_count(model: str, domain: list[Any] | None = None) -> int:
        """Count records matching `domain` on an allowlisted model -
        returns one integer, never rows. Use this for "how many" instead
        of paging `search_read` and counting the results yourself.

        model: must be in this server's `allowed_models` allowlist.
        domain: Odoo domain, same rules as `search_read`'s (every field
            clause must be in the read allowlist unless `read` is "all").
        """
        access = _require_model_access(models, model)
        domain = domain or []
        _check_domain_fields(access, model, domain)
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
        """Group + aggregate an allowlisted model in one call - e.g.
        "partners by country" or "sum of amount_total per state". Use
        this instead of `search_read`-ing every row and totaling them
        yourself; it returns one row per group, not one per record.

        model: must be in this server's `allowed_models` allowlist.
        fields: aggregates, Odoo's own syntax - a bare field name (e.g.
            `"amount_total"`, default aggregation) or `"field:agg"`
            (e.g. `"amount_total:sum"`). Each field name must be in the
            model's read allowlist unless `read` is "all".
        groupby: field name(s) to group by; a date/datetime field may
            add a granularity, e.g. `"date_order:month"`. Same allowlist
            rule as `fields`.
        domain: Odoo domain, same rules as `search_read`'s.
        limit: max groups returned (default 20, capped at 100) - this
            limits the number of *groups*, not the number of underlying
            records.
        offset: pagination offset over groups (default 0).
        """
        access = _require_model_access(models, model)
        domain = domain or []
        _check_domain_fields(access, model, domain)
        _check_aggregate_fields(access, model, fields)
        _check_groupby_fields(access, model, groupby)
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
        """Resolve a display-name substring to `(id, display_name)`
        pairs on an allowlisted model. Use this to find a record's id
        (e.g. "which partner is 'Azure'?", or resolving a many2one label
        seen in a `search_read` result) instead of a full `search_read`.

        model: must be in this server's `allowed_models` allowlist.
        name: the name pattern to match against `display_name`.
        domain: Odoo domain, same rules as `search_read`'s - narrows the
            candidates further (e.g. only companies).
        operator: domain operator for matching `name` (default "ilike").
        limit: max pairs returned (default 20, capped at 100).
        """
        access = _require_model_access(models, model)
        domain = domain or []
        _check_domain_fields(access, model, domain)
        limit = min(limit, _MAX_LIMIT)
        return _get_backend().name_search(
            model, name=name, domain=domain, operator=operator, limit=limit
        )

    if "create" in allowed_write_tools:

        @mcp.tool()
        def create(model: str, values: dict[str, Any], confirm: bool = False) -> dict[str, Any]:
            """Create one record of an allowlisted model. Write tool -
            only available on servers whose `write_tools` allowlist
            includes "create" (config/servers.yaml).

            Every key of `values` must be in that model's write allowlist
            (`allowed_models.<model>.write`); missing/empty `write` means
            no write access at all, even with this tool enabled.

            confirm: defaults to false - returns a preview only, does
            **not** write to Odoo. Pass confirm=true to actually create
            the record; Odoo's own access rights for the API key's user
            still apply - insufficient permissions raise a clear error,
            never a silent no-op.
            """
            access = _require_model_access(models, model)
            _check_write_fields(access, model, values)
            if not confirm:
                return {"confirmed": False, "created": False, "preview": values}
            new_id = _get_backend().create(model, values)
            return {"confirmed": True, "created": True, "id": new_id, **values}

    if "write" in allowed_write_tools:

        @mcp.tool()
        def write(
            model: str, ids: list[int], values: dict[str, Any], confirm: bool = False
        ) -> dict[str, Any]:
            """Update existing record(s) of an allowlisted model. Write
            tool - only available on servers whose `write_tools`
            allowlist includes "write" (config/servers.yaml).

            ids: record ids to update - must be non-empty, at most 100.
            Every key of `values` must be in that model's write allowlist
            (`allowed_models.<model>.write`); missing/empty `write` means
            no write access at all, even with this tool enabled.

            confirm: defaults to false - returns a preview only, does
            **not** write to Odoo. Pass confirm=true to actually update
            the record(s); Odoo's own access rights for the API key's
            user still apply - insufficient permissions raise a clear
            error, never a silent no-op.
            """
            if not ids:
                raise ValueError("'ids' must not be empty.")
            if len(ids) > _MAX_WRITE_IDS:
                raise ValueError(f"'ids' must not contain more than {_MAX_WRITE_IDS} ids.")
            access = _require_model_access(models, model)
            _check_write_fields(access, model, values)
            if not confirm:
                return {"confirmed": False, "written": False, "ids": ids, "preview": values}
            ok = _get_backend().write(model, ids, values)
            return {"confirmed": True, "written": ok, "ids": ids, **values}

    return mcp
