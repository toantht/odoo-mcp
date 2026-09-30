"""Phase 0 smoke test: one-off JSON-2 call against the Odoo 19 pilot server.

Not part of the MCP stack yet - just proves the API key + JSON-2 endpoint
work before any code depends on them. Uses only the stdlib (no httpx yet;
that lands in Phase 2's Json2Backend).

Usage:
    Copy `.env.example` to `.env` and fill in real values, then:

        uv run --env-file .env python scripts/phase0_smoke_json2.py

    `uv run` does NOT auto-load `.env` files - `--env-file` is required.
    (Or set $env:ODOO_URL / $env:ODOO_API_KEY / $env:ODOO_DB directly in
    the shell instead of using a .env file.)

Exit code 0 + printed partner list = pass.
Non-zero exit + printed status/body = clear ACL/auth/network error (also a
valid, informative outcome for Phase 0).
"""

from __future__ import annotations
from dotenv import load_dotenv

import json
import os
import sys
import urllib.error
import urllib.request

load_dotenv()


def main() -> int:
    url = os.environ.get("ODOO_URL", "").rstrip("/")
    api_key = os.environ.get("ODOO_API_KEY", "")
    db = os.environ.get("ODOO_DB", "").strip()

    if not url or not api_key:
        print("Missing ODOO_URL or ODOO_API_KEY env vars.", file=sys.stderr)
        return 2

    endpoint = f"{url}/json/2/res.partner/search_read"
    payload = json.dumps(
        {
            "domain": [],
            "fields": ["id", "name", "email"],
            "limit": 3,
        }
    ).encode("utf-8")

    headers = {
        "Authorization": f"bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "odoo-mcp-phase0-smoke/0.1",
    }
    if db:
        headers["X-Odoo-Database"] = db

    request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            print(f"HTTP {response.status}")
            print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
            return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code} {exc.reason}", file=sys.stderr)
        print(body, file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"Network error calling {endpoint}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
