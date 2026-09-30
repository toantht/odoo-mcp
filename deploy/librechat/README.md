# LibreChat, as an MCP client for odoo-mcp

Self-host web UI (Claude API + this repo's Odoo tools), one gateway
connection per LibreChat user, each authenticated with their own Odoo
consent. See [`blueprint-librechat.md`](../../blueprint-librechat.md)
for the full design. This directory only adds runnable deploy files -
it does not change the gateway (`src/odoo_mcp`) or the addon
(`addon/odoo_mcp`).

This is a separate stack from the repo-root `docker-compose.yml`
(its own MongoDB + Meilisearch). It never talks to the gateway
container directly - it must call the gateway's public
`ODOO_MCP_PUBLIC_URL`, the same URL Claude.ai would use.

## Setup

```bash
cd deploy/librechat
cp .env.example .env                       # fill in secrets (see comments in the file)
cp librechat.yaml.example librechat.yaml    # edit the gateway url(s)
docker compose up -d
curl http://127.0.0.1:${PORT:-3080}/api/health
```

## Gateway-side prerequisite

On the **gateway's** `.env` (repo root, not this directory), add this
LibreChat instance's public hostname to the OAuth redirect allowlist:

```dotenv
ODOO_MCP_OAUTH_REDIRECT_HOSTS=chat.example.com
```

`localhost`/`127.0.0.1` are already allowed by default, so a local
trial at `http://localhost:3080` needs no gateway change. The gateway
must also already have OAuth enabled (`ODOO_MCP_ALLOW_SHARED_KEY`
unset), a real `ODOO_MCP_PUBLIC_URL`, and `addon/odoo_mcp` installed on
each Odoo server listed in `librechat.yaml` - see blueprint-v3.md
Phase 12 and the repo-root README.

## Checklist (from blueprint-librechat.md)

1. `curl {ODOO_MCP_PUBLIC_URL}/healthz` - `oauth_enabled: true`, the
   server(s) you configured are listed.
2. Log into LibreChat as user A, open MCP Settings, Authenticate
   `odoo_a` - the browser should go through Odoo `/web/login` (if no
   session yet) then the addon's consent page, not a redirect_uri
   error.
3. Click Allow - user A's Odoo Account Security now has a new API key
   named `Claude MCP`; LibreChat shows the server as Connected.
4. From user A's chat, call a read tool (`search_read`) - the data
   returned respects user A's own Odoo ACL (test with a
   permission-restricted user to confirm).
5. Log into LibreChat as user B, Authenticate again - confirm B gets
   its own token/API key (not reusing A's), and sees different data if
   B's Odoo ACL differs from A's.
6. Click Deny on a fresh Authenticate attempt - confirm no new
   `Claude MCP` key is created and LibreChat reports an authorization
   error rather than Connected.

## Out of scope here

Same as blueprint-librechat.md: no audit log by Odoo `uid`, no
monitoring/alerting, and no multi-replica gateway support (the vault
and pending-OAuth-transaction state are single-process).
