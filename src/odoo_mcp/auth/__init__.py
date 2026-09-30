"""Phase 12: OAuth 2.1 authorization server (Claude <-> gateway) plus a
SQLite-backed vault (gateway <-> Odoo API keys) - see blueprint-v3.md.

`vault.Vault` stores dynamically-registered OAuth clients and issued
access/refresh tokens (Fernet-encrypted Odoo API key, never the raw key,
at rest). `provider.OdooMcpAuthProvider` implements the `mcp` SDK's
`OAuthAuthorizationServerProvider` protocol on top of it, redirecting the
actual "who are you" question to each Odoo server's own `/odoo_mcp/authorize`
consent page (the `addon/odoo_mcp` addon) instead of asking for a password
itself. `gateway/http.py` is the only caller - it mounts
`provider.build_routes(...)` once for the whole gateway and wraps each
`/mcp/{server_id}` endpoint with a bearer-token check scoped to that one
server.
"""
