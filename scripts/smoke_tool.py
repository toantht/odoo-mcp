"""Phase 2 smoke test: exercise `search_partners` / `get_partner` against
the real `Json2Backend` (Odoo 19 pilot server).

Same tool functions as Phase 1's `FakeBackend` run - only the backend
changed, proving tools don't care which one they get.

Usage:
    uv run --env-file .env python scripts/smoke_tool.py [name]
    uv run --env-file .env python scripts/smoke_tool.py search [name]
    uv run --env-file .env python scripts/smoke_tool.py get <id>
    uv run --env-file .env python -m scripts.smoke_tool [name]

    `uv run` does NOT auto-load `.env` files - `--env-file` is required
    (or export ODOO_URL / ODOO_API_KEY / ODOO_DB in the shell instead).

Exit code 0 + printed partner(s) (JSON) = pass.
"""

from __future__ import annotations

import json
import sys

from odoo_mcp.backends.json2 import Json2Backend, Json2Error
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
        backend = Json2Backend.from_env()
    except Json2Error as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    try:
        if command == "get":
            if not rest:
                print("Usage: smoke_tool.py get <id>", file=sys.stderr)
                return 2
            result = get_partner(backend, int(rest[0]))
        else:
            name = rest[0] if rest else None
            result = search_partners(backend, name=name)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Json2Error as exc:
        print(f"Odoo API error: {exc}", file=sys.stderr)
        return 1
    finally:
        backend.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
