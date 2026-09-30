"""Simple read-only MCP server for Odoo 19+ (External JSON-2 API).

Env vars: ODOO_URL, ODOO_DB, ODOO_API_KEY   (ODOO_USER no longer needed)
Run:      python odoo_mcp.py   ->  http://0.0.0.0:8000/mcp
"""

import json
import os
import urllib.error
import urllib.request
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

ODOO_URL = os.environ["ODOO_URL"].rstrip("/")
ODOO_DB = os.environ["ODOO_DB"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

mcp = FastMCP("odoo", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


def _call(model: str, method: str, params: dict):
    """POST {ODOO_URL}/json/2/<model>/<method> with a bearer API key."""
    req = urllib.request.Request(
        f"{ODOO_URL}/json/2/{model}/{method}",
        data=json.dumps(params).encode(),
        headers={
            "Authorization": f"bearer {ODOO_API_KEY}",
            "X-Odoo-Database": ODOO_DB,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"Odoo HTTP {e.code} on {req.full_url}: {e.read().decode()[:500]}"
        )


@mcp.tool()
def odoo_ping() -> str:
    """Test the connection: fetch one user record from Odoo."""
    users = _call(
        "res.users", "search_read", {"domain": [], "fields": ["login"], "limit": 1}
    )
    return json.dumps({"ok": True, "sample_user": users}, ensure_ascii=False)


@mcp.tool()
def odoo_search(
    model: str,
    domain: list | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
) -> str:
    """Search records (read-only).

    model: e.g. 'res.partner', 'hr.employee', 'sale.order'
    domain: Odoo domain, e.g. [["active", "=", true]]
    fields: field names to return, e.g. ["name", "work_email"]
    limit: max records (capped at 100)
    """
    params = {"domain": domain or [], "limit": min(limit, 100)}
    if fields:
        params["fields"] = fields
    return json.dumps(
        _call(model, "search_read", params), ensure_ascii=False, default=str
    )


@mcp.tool()
def odoo_count(model: str, domain: list | None = None) -> int:
    """Count records matching a domain."""
    return _call(model, "search_count", {"domain": domain or []})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
