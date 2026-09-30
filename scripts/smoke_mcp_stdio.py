"""Phase 3 smoke test: drive `gateway/stdio.py` as a real MCP client would.

Spawns the stdio server as a subprocess, runs the MCP lifecycle
(`initialize` -> `tools/list` -> `tools/call`), and checks the `ping`
tool replies "pong". No Odoo involved - this only proves the MCP
transport itself works, same thing Cursor/Claude Desktop will do.

Usage:
    uv run python scripts/smoke_mcp_stdio.py
    uv run python -m scripts.smoke_mcp_stdio

Exit code 0 + "pong" printed = pass.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


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
            if "ping" not in tool_names:
                print("FAIL: 'ping' tool not advertised", file=sys.stderr)
                return 1

            result = await session.call_tool("ping", {})
            text = "".join(
                block.text for block in result.content if block.type == "text"
            )
            print(f"tools/call ping -> {text!r}")
            if result.isError or text != "pong":
                print("FAIL: ping did not return 'pong'", file=sys.stderr)
                return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
