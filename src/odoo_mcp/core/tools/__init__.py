"""Tool functions: thin wrappers that call `Backend` methods only.

No tool here knows about JSON-2, XML-RPC, or ORM - see `core.backend`.
"""

from __future__ import annotations

from typing import Any

from odoo_mcp.core.backend import Backend

_PARTNER_FIELDS = ["id", "name", "email"]


def search_partners(
    backend: Backend,
    name: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search `res.partner` by (partial) name.

    name: optional case-insensitive substring match; omit to list all.
    limit: max records returned.
    """
    domain = [("name", "ilike", name)] if name else []
    return backend.search_read(
        "res.partner", domain=domain, fields=_PARTNER_FIELDS, limit=limit
    )


def get_partner(backend: Backend, partner_id: int) -> dict[str, Any] | None:
    """Read one `res.partner` by id. Returns None if not found."""
    records = backend.read("res.partner", [partner_id], fields=_PARTNER_FIELDS)
    return records[0] if records else None


def create_partner(
    backend: Backend,
    name: str,
    email: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create one `res.partner`. Preview-only unless `confirm=True`
    (Phase 9 write-tool contract).

    confirm=False (default): never calls `backend.create` - returns the
    values that *would* be written, so nothing reaches Odoo.
    confirm=True: actually creates the record. Odoo's own ACLs for the
    API key's user still apply - insufficient rights raise a normal
    backend error (`Json2Error`/`XmlRpcError`), never silently ignored.
    """
    values: dict[str, Any] = {"name": name}
    if email:
        values["email"] = email

    if not confirm:
        return {"confirmed": False, "created": False, "preview": values}

    partner_id = backend.create("res.partner", values)
    return {"confirmed": True, "created": True, "id": partner_id, **values}
