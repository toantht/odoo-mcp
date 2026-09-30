"""Phase 6 smoke test: drive `gateway/http.py` as a real HTTP MCP client would.

Spawns the HTTP gateway (`python -m odoo_mcp.gateway.http`) as a
subprocess on an ephemeral local port, pointed at the shipped
`config/servers.example.yaml` via `ODOO_MCP_CONFIG` - no real Odoo /
`ODOO_API_KEY` needed, same as `smoke_mcp_stdio.py`: only `tools/list`
and `ping` are exercised, and neither touches the `Backend`.

Checks:
- `GET /healthz` returns 200 and lists the example registry's server id
  (`odoo_a`).
- The Streamable HTTP MCP endpoint at `/mcp/odoo_a` completes
  `initialize` -> `tools/list` -> `tools/call ping` and gets "pong" back
  - the same MCP lifecycle `smoke_mcp_stdio.py` checks over stdio,
  just over HTTP this time.

`gateway/stdio.py` is untouched by any of this; both gateways run side
by side (Phase 6 goal: "stdio vẫn chạy song song").

Usage:
    uv run python scripts/smoke_mcp_http.py

Exit code 0 + "pong" printed = pass.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_EXAMPLE_REGISTRY = Path(__file__).resolve().parent.parent / "config" / "servers.example.yaml"
_SERVER_ID = "odoo_a"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_healthy(base_url: str, client: httpx.AsyncClient, timeout: float = 10.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    last_exc: Exception | None = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            response = await client.get(f"{base_url}/healthz")
            if response.status_code == 200:
                return response.json()
        except httpx.TransportError as exc:
            last_exc = exc
        await asyncio.sleep(0.2)
    raise RuntimeError(f"Gateway never became healthy: {last_exc}")


async def main() -> int:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "ODOO_MCP_CONFIG": str(_EXAMPLE_REGISTRY),
        "ODOO_MCP_HTTP_PORT": str(port),
        "ODOO_MCP_HTTP_HOST": "127.0.0.1",
    }

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "odoo_mcp.gateway.http",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with httpx.AsyncClient() as http_client:
            health = await _wait_healthy(base_url, http_client)
            print(f"GET /healthz -> {health}")
            if _SERVER_ID not in health.get("servers", []):
                print(f"FAIL: '{_SERVER_ID}' not in /healthz servers", file=sys.stderr)
                return 1

            async with streamable_http_client(f"{base_url}/mcp/{_SERVER_ID}") as (
                read,
                write,
                _get_session_id,
            ):
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
    finally:
        process.terminate()
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            stdout, _ = await process.communicate()
        if stdout:
            print(stdout.decode(errors="replace"), file=sys.stderr, end="")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
