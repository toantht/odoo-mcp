"""MCP transports (stdio, later HTTP) that expose tools to MCP clients.

Phase 3 shipped `gateway/stdio.py` with a single `ping` health-check
tool. Phase 4 wires in the real read-only tools (`search_partners`,
`get_partner`) backed by `Json2Backend`, closing the end-to-end loop:
AI client -> MCP -> Backend -> Odoo.
"""
