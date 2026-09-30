"""Phase 3+4: MCP stdio server, wired to a real `Backend`.

Phase 3 proved the MCP lifecycle (`initialize` -> `tools/list` ->
`tools/call`) with a standalone `ping` tool. Phase 4 added real,
read-only tools calling `core.tools` -> `Backend` -> the pilot Odoo
server, closing the end-to-end loop: AI client -> MCP -> Backend ->
Odoo. Phase 11 replaced those fixed, per-model tools with generic ones
(`list_models`, `describe_model`, `search_read`, `create`, `write` -
see `core.mcp_server`), gated by two registry-driven allowlists:
`backends.write_tools_from_env` (which write tools exist at all) and
`backends.allowed_models_from_env` (which models/fields they may touch).
Phase 8 lets the `Backend` itself be either `Json2Backend` (Odoo 19+) or
`XmlRpcBackend` (Odoo <=18) - same tools either way, only the registry
entry's `backend:` value changes.

Tool registration itself lives in `core.mcp_server.build_mcp_server`,
shared with the Phase 6 HTTP gateway (`gateway/http.py`) - this module
only decides *which* single `Backend` to bind it to (env/registry via
`backends.backend_from_env`), created lazily (on first real tool call)
so `tools/list` and `ping` still work even without a configured pilot
server.

Run directly:
    uv run odoo-mcp-stdio
    uv run python -m odoo_mcp.gateway.stdio

Configure as an MCP server in Cursor/Claude Desktop with:
    command: uv
    args: ["run", "--directory", "<repo path>", "odoo-mcp-stdio"]

See README.md for full client config examples.
"""

from __future__ import annotations

from odoo_mcp.backends import allowed_models_from_env, backend_from_env, write_tools_from_env
from odoo_mcp.core.mcp_server import build_mcp_server

mcp = build_mcp_server(
    "odoo-mcp",
    backend_from_env,
    write_tools=write_tools_from_env(),
    allowed_models=allowed_models_from_env(),
)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
