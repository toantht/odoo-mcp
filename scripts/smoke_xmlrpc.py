"""Phase 8 smoke test: exercise `search_partners` / `get_partner` against
a real Odoo <=18 pilot server over `XmlRpcBackend`.

Same tool functions `scripts/smoke_tool.py` runs against `Json2Backend` -
only the backend changed, proving tools don't care which one they get
(see also `tests/test_backend_contract.py` for the mocked version of
this same claim).

Requires an xmlrpc entry in `config/servers.yaml` (`url`, `db`, `login`,
`backend: xmlrpc` - see `config/servers.example.yaml`'s `odoo_b`) and
`ODOO_MCP_SERVER=<that id>` + `ODOO_API_KEY` in `.env`, or the raw
`ODOO_URL`/`ODOO_DB`/`ODOO_LOGIN`/`ODOO_API_KEY` env vars directly.

Usage:
    uv run --env-file .env python scripts/smoke_xmlrpc.py [name]
    uv run --env-file .env python scripts/smoke_xmlrpc.py search [name]
    uv run --env-file .env python scripts/smoke_xmlrpc.py get <id>

    `uv run` does NOT auto-load `.env` files - `--env-file` is required
    (or export the env vars in the shell instead).

Exit code 0 + printed partner(s) (JSON) = pass.
"""

from __future__ import annotations

import json
import sys

from odoo_mcp.backends.xmlrpc import XmlRpcBackend, XmlRpcError
from odoo_mcp.core.tools import get_partner, search_partners

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    # Windows consoles default to a codepage (e.g. cp1252) that can't
    # encode non-ASCII partner names/emails - reconfigure instead of
    # requiring callers to set PYTHONIOENCODING=utf-8 themselves.
    sys.stdout.reconfigure(encoding="utf-8")


def main(argv: list[str]) -> int:
    args = argv[1:]
    command, rest = (args[0], args[1:]) if args and args[0] in ("search", "get") else (
        "search",
        args,
    )

    try:
        backend = XmlRpcBackend.from_env()
    except XmlRpcError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    try:
        if command == "get":
            if not rest:
                print("Usage: smoke_xmlrpc.py get <id>", file=sys.stderr)
                return 2
            result = get_partner(backend, int(rest[0]))
        else:
            name = rest[0] if rest else None
            result = search_partners(backend, name=name)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except XmlRpcError as exc:
        print(f"Odoo XML-RPC error: {exc}", file=sys.stderr)
        return 1
    finally:
        backend.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
