"""Phase 9 smoke test: exercise the `create_partner` write tool against a
real Odoo pilot server (whichever backend `ODOO_MCP_SERVER` points at -
`Json2Backend` or `XmlRpcBackend`, via `backends.backend_from_env`).

Confirms the plan's "Test / xong khi" for Phase 9 against a real server:
- no `--confirm` -> preview only, nothing written to Odoo.
- `--confirm` + the API key's user has create rights on `res.partner`
  -> a real record is created, id printed.
- `--confirm` + insufficient rights -> a clear Odoo/backend error, not a
  silent no-op.

Usage:
    uv run --env-file .env python scripts/smoke_write_tool.py "Test Co"
    uv run --env-file .env python scripts/smoke_write_tool.py "Test Co" test@example.com
    uv run --env-file .env python scripts/smoke_write_tool.py "Test Co" test@example.com --confirm

    `uv run` does NOT auto-load `.env` files - `--env-file` is required
    (or export the env vars in the shell instead).

Exit code 0 + printed result (JSON) = pass. Prefer running without
`--confirm` first to sanity-check the preview before actually writing.
"""

from __future__ import annotations

import json
import sys

from odoo_mcp.backends import Json2Error, XmlRpcError, backend_from_env
from odoo_mcp.core.tools import create_partner

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    # Windows consoles default to a codepage (e.g. cp1252) that can't
    # encode non-ASCII partner names/emails - reconfigure instead of
    # requiring callers to set PYTHONIOENCODING=utf-8 themselves.
    sys.stdout.reconfigure(encoding="utf-8")


def main(argv: list[str]) -> int:
    args = argv[1:]
    confirm = "--confirm" in args
    positional = [a for a in args if a != "--confirm"]

    if not positional:
        print(
            'Usage: smoke_write_tool.py "<name>" [email] [--confirm]',
            file=sys.stderr,
        )
        return 2
    name = positional[0]
    email = positional[1] if len(positional) > 1 else None

    try:
        backend = backend_from_env()
    except (Json2Error, XmlRpcError) as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2

    try:
        result = create_partner(backend, name=name, email=email, confirm=confirm)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (Json2Error, XmlRpcError) as exc:
        print(f"Odoo error (e.g. insufficient permissions): {exc}", file=sys.stderr)
        return 1
    finally:
        close = getattr(backend, "close", None)
        if close:
            close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
