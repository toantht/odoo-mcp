"""Phase 4 smoke test: full "AI -> MCP -> Backend -> Odoo" loop.

Spawns `gateway/stdio.py` as a real MCP client would (same lifecycle as
`smoke_mcp_stdio.py`), then calls the Phase 11 generic tool `search_read`
against `res.partner` and checks it returns actual data from the Odoo
pilot server (not fake/hard-coded data).

Requires a configured `.env` (`ODOO_URL` / `ODOO_API_KEY` / optional
`ODOO_DB`) - the spawned server loads it itself via
`Json2Backend.from_env()`, so no `--env-file` is needed here. It also
requires the API key's Odoo user to have read access on `res.partner`
(`id`/`name`/`email`) - Phase 16 moved that check onto Odoo's own
`ir.model.access`/`ir.rule`, so an insufficiently-permissioned key now
fails with an Odoo `AccessError`, not a local allowlist `ValueError`.

Usage:
    uv run python scripts/smoke_mcp_e2e.py
    uv run python -m scripts.smoke_mcp_e2e

Exit code 0 + printed partner data = pass.
"""

from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    # Windows consoles default to a codepage (e.g. cp1252) that can't
    # encode non-ASCII partner names/emails - reconfigure instead of
    # requiring callers to set PYTHONIOENCODING=utf-8 themselves.
    sys.stdout.reconfigure(encoding="utf-8")


async def main() -> int:
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "odoo_mcp.gateway.stdio"],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            tool_names = [tool.name for tool in tools]
            print(f"tools/list -> {tool_names}")
            for expected in ("describe_model", "search_read"):
                if expected not in tool_names:
                    print(f"FAIL: '{expected}' tool not advertised", file=sys.stderr)
                    return 1

            search_result = await session.call_tool(
                "search_read",
                {"model": "res.partner", "fields": ["id", "name", "email"], "limit": 3},
            )
            if search_result.isError:
                print(f"FAIL: search_read errored: {search_result.content}", file=sys.stderr)
                return 1
            partners = search_result.structuredContent
            print(f"tools/call search_read -> {json.dumps(partners, ensure_ascii=False)}")

            records = partners.get("result") if isinstance(partners, dict) else partners
            if not records:
                print("FAIL: search_read returned no partners", file=sys.stderr)
                return 1

            first_id = records[0]["id"]
            get_result = await session.call_tool(
                "search_read",
                {
                    "model": "res.partner",
                    "domain": [["id", "=", first_id]],
                    "fields": ["id", "name", "email"],
                    "limit": 1,
                },
            )
            if get_result.isError:
                print(f"FAIL: search_read (by id) errored: {get_result.content}", file=sys.stderr)
                return 1
            print(
                f"tools/call search_read(id={first_id}) -> "
                f"{json.dumps(get_result.structuredContent, ensure_ascii=False)}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
