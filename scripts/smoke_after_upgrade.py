"""Phase 10 smoke test: one script to run after an Odoo upgrade (version
bump / module update) or after rotating `ODOO_API_KEY` - instead of
re-running every earlier phase's smoke test by hand.

Exercises the same read contract as `scripts/smoke_tool.py` /
`scripts/smoke_xmlrpc.py` (`search_partners` / `get_partner` via
`core.tools`), but through `backends.backend_from_env` - whichever
backend (`json2` or `xmlrpc`) the current `.env`/registry points at, so
this one script works for either kind of pilot server. It also:

- best-effort prints the installed version of the `base` module
  (`ir.module.module`) - a quick eyeball check that an upgrade actually
  landed. Never fails the run on its own: some API key users won't have
  read access to `ir.module.module`, and that's not what this script
  is checking.
- if `create_partner` is allowlisted for the resolved server
  (`backends.write_tools_from_env`), makes one preview-only
  (`confirm=False`) call - proves the write-tool config (allowlist +
  ACLs) still resolves correctly post-upgrade without writing anything.

See README "Phase 10 - Ops checklist" for when to run this.

Usage:
    uv run --env-file .env python scripts/smoke_after_upgrade.py

    `uv run` does NOT auto-load `.env` files - `--env-file` is required
    (or export the env vars in the shell instead).

Exit code 0 + printed summary = pass. Non-zero = a clear connectivity/
auth/ACL error - the actual point of running this after an upgrade or
key rotation, before trusting the server with real client traffic.
"""

from __future__ import annotations

import json
import sys

from odoo_mcp.backends import (
    Json2Error,
    XmlRpcError,
    backend_from_env,
    write_tools_from_env,
)
from odoo_mcp.core.backend import Backend
from odoo_mcp.core.tools import create_partner, get_partner, search_partners

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    # Windows consoles default to a codepage (e.g. cp1252) that can't
    # encode non-ASCII partner names/emails - reconfigure instead of
    # requiring callers to set PYTHONIOENCODING=utf-8 themselves.
    sys.stdout.reconfigure(encoding="utf-8")


def _installed_base_version(backend: Backend) -> str | None:
    """Best-effort: read the installed version of the `base` module.
    Returns None (never raises) on any error - missing ACL on
    ir.module.module is not what this script is checking."""
    try:
        records = backend.search_read(
            "ir.module.module",
            domain=[("name", "=", "base")],
            fields=["installed_version"],
            limit=1,
        )
        return records[0].get("installed_version") if records else None
    except Exception:
        return None


def main() -> int:
    try:
        backend = backend_from_env()
    except (Json2Error, XmlRpcError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    try:
        version = _installed_base_version(backend)
        print(f"Odoo 'base' module version: {version or '(unknown - not checked)'}")

        partners = search_partners(backend, limit=3)
        print(f"search_partners(limit=3) -> {json.dumps(partners, ensure_ascii=False)}")
        if not partners:
            print(
                "FAIL: search_partners returned no results - check ACLs/data "
                "on this server.",
                file=sys.stderr,
            )
            return 1

        first_id = partners[0]["id"]
        partner = get_partner(backend, first_id)
        print(f"get_partner({first_id}) -> {json.dumps(partner, ensure_ascii=False)}")
        if not partner:
            print(f"FAIL: get_partner({first_id}) returned nothing.", file=sys.stderr)
            return 1

        allowed_writes = write_tools_from_env()
        if "create_partner" in allowed_writes:
            preview = create_partner(
                backend, name="odoo-mcp smoke_after_upgrade", confirm=False
            )
            print(
                "create_partner(confirm=False) preview -> "
                f"{json.dumps(preview, ensure_ascii=False)}"
            )
            if preview.get("created"):
                print(
                    "FAIL: preview call unexpectedly wrote a record.",
                    file=sys.stderr,
                )
                return 1
        else:
            print(
                "create_partner not allowlisted for this server - skipping "
                "write-tool config check."
            )
    except (Json2Error, XmlRpcError) as exc:
        print(f"Odoo error: {exc}", file=sys.stderr)
        return 1
    finally:
        close = getattr(backend, "close", None)
        if close:
            close()

    print("OK - read path (and write-tool config, if allowlisted) confirmed working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
