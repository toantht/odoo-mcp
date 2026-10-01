# odoo-mcp

MCP gateway for Odoo. See `blueprint.md`.

```bash
uv sync
uv run odoo-mcp
```

## Phase 3 - MCP stdio server (`ping` only)

`src/odoo_mcp/gateway/stdio.py` started as a minimal MCP server over
stdio with one tool, `ping` -> `"pong"`, proving the MCP lifecycle
(`initialize` -> `tools/list` -> `tools/call`) works before any Odoo
backend was wired in.

## Phase 4 - Real read-only tools, end-to-end

The same stdio server now also exposes `search_partners` and
`get_partner`, backed by the real `Json2Backend` (created lazily from
`.env` on first call). This closes the loop: **AI client -> MCP ->
Backend -> Odoo**.

Run it directly:

```bash
uv run odoo-mcp-stdio
# or
uv run python -m odoo_mcp.gateway.stdio
```

Requires `.env` (`ODOO_URL` / `ODOO_API_KEY` / optional `ODOO_DB`, see
`.env.example`) for `search_partners` / `get_partner`; `ping` and
`tools/list` work even without it.

Automated smoke tests:

```bash
# MCP lifecycle only, calls "ping" - no Odoo required
uv run python scripts/smoke_mcp_stdio.py

# Full E2E: MCP client -> stdio gateway -> Json2Backend -> real Odoo
uv run python scripts/smoke_mcp_e2e.py
```

### Configure in Cursor

Add to `.cursor/mcp.json` (project) or the global MCP settings:

```json
{
  "mcpServers": {
    "odoo-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "D:/Projects/odoo-mcp", "odoo-mcp-stdio"]
    }
  }
}
```

### Configure in Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "odoo-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "D:/Projects/odoo-mcp", "odoo-mcp-stdio"]
    }
  }
}
```

Restart the client, then check its MCP/tools panel for `odoo-mcp` with
three tools - `ping`, `search_partners`, `get_partner` - and try asking
it to "find customer X" to confirm it returns real Odoo data.

## Phase 5 - Server registry (config instead of hard-coded env)

`ODOO_URL` / `ODOO_DB` (Phase 2-4) still work, but you can now point at
a server by id from a YAML registry instead:

```bash
cp config/servers.example.yaml config/servers.yaml
# edit config/servers.yaml with your pilot server's url/db/version
```

```dotenv
# .env
ODOO_API_KEY=...
ODOO_MCP_SERVER=odoo_a   # id from config/servers.yaml
```

`Json2Backend.from_env()` reads the registry when `ODOO_MCP_SERVER` is
set (falling back to `ODOO_URL`/`ODOO_DB` otherwise), so switching
pilots is a YAML + env change, not a code change. `config/servers.yaml`
is git-ignored - only `servers.example.yaml` is committed. See
`src/odoo_mcp/config/registry.py`.

Unit tests for the loader (no network, no real Odoo):

```bash
uv run pytest tests/
```

## Phase 6 - HTTP gateway (`/mcp/{server_id}`, log + rate limit)

`gateway/http.py` mounts one Streamable HTTP MCP endpoint **per server**
in `config/servers.yaml` - `/mcp/odoo_a`, `/mcp/odoo_b`, ... - instead
of a single endpoint with a `server` parameter on every tool (safer:
a client can't accidentally mix data between servers). Same tools as
`stdio.py` (`ping`, `search_partners`, `get_partner`); `stdio.py` is
unaffected and keeps working side by side.

Run it:

```bash
uv run odoo-mcp-http
# or
uv run uvicorn --factory odoo_mcp.gateway.http:create_app
```

```bash
curl http://127.0.0.1:8000/healthz
# {"status": "ok", "servers": ["odoo_a"]}
```

Env vars (all optional beyond what Phase 2-5 already need):

- `ODOO_MCP_HTTP_HOST` / `ODOO_MCP_HTTP_PORT` - default `127.0.0.1:8000`.
- `ODOO_MCP_RATE_LIMIT` / `ODOO_MCP_RATE_LIMIT_WINDOW` - simple in-memory
  fixed-window rate limit on `/mcp/*` requests (default 60 req / 60s),
  keyed by API key (hashed - the raw key is never kept or logged) when
  the request has an `Authorization` header, else by client ip.
  `/healthz` is never rate-limited.

Every `/mcp/*` request logs one line - method, path, server id,
JSON-RPC method, tool name (for `tools/call`), response status, client
ip. Headers (and therefore the API key) are never logged.

Automated smoke test (spawns the gateway on an ephemeral port against
`config/servers.example.yaml` - no real Odoo needed, same as
`smoke_mcp_stdio.py`):

```bash
uv run python scripts/smoke_mcp_http.py
```

Only `backend: json2` servers are mounted; `xmlrpc` (Phase 8) is
skipped with a log line.

## Phase 7 - Docker + HTTPS (reverse proxy)

`Dockerfile` packages the HTTP gateway only (`odoo-mcp-http`) -
`stdio.py` stays a local subprocess, not a container. The container
itself only ever speaks plain HTTP; **HTTPS is terminated by a reverse
proxy**, not invented inside the app (the plan deliberately avoids
rolling custom TLS/CA handling here - use infra that already does this
well: Caddy, Traefik, nginx, ...).

```bash
cp .env.example .env               # then fill in ODOO_API_KEY (+ ODOO_URL/ODOO_DB, or ODOO_MCP_SERVER)
cp config/servers.example.yaml config/servers.yaml   # then edit it for your pilot server

docker compose up --build
curl http://127.0.0.1:8000/healthz
```

By default `docker-compose.yml` publishes the gateway only on
`127.0.0.1:8000` (host-local, no TLS) - point an MCP client at it
directly for local testing, or put a reverse proxy in front for a real
public URL.

### Option A - reverse proxy you already run

If this host already runs Traefik/nginx/Caddy for other services, skip
straight to pointing it at `127.0.0.1:8000` (proxy_pass / matching
router config) - don't add a second HTTPS terminator on the same host.

### Option B - bundled Caddy (automatic HTTPS, no existing proxy)

`docker-compose.yml` includes an optional `caddy` service (compose
profile `https`) that gets free automatic HTTPS (Let's Encrypt) from
just a domain name - no manual certs. Needs a real DNS name pointed at
this host with ports 80/443 reachable from the internet.

```bash
cp Caddyfile.example Caddyfile      # edit the domain
docker compose --profile https up --build
```

`Caddyfile` and `config/servers.yaml` are both git-ignored (may contain
a real domain / internal URLs) - only the `.example` files are
committed.

### Config recap

- `.env` - same `ODOO_API_KEY` (+ `ODOO_URL`/`ODOO_DB` or
  `ODOO_MCP_SERVER`) as every earlier phase, passed to the container via
  `env_file:` - never baked into the image (see `.dockerignore`).
- `config/servers.yaml` - bind-mounted read-only into the container at
  `/app/config/servers.yaml` (Phase 5 registry); edit the host file and
  `docker compose restart gateway` to pick up changes.
- `ODOO_MCP_HTTP_HOST`/`ODOO_MCP_HTTP_PORT` default to `0.0.0.0:8000`
  inside the container (set in the `Dockerfile`, unlike the
  `127.0.0.1` local-dev default in Phase 6) - what's actually reachable
  is controlled by `docker-compose.yml`'s `ports:` / the reverse proxy,
  not this.

**Test / done when:** `docker compose up` builds and starts the
container, `curl http://127.0.0.1:8000/healthz` returns
`{"status": "ok", ...}`, and an MCP client configured with a
Streamable HTTP URL (`http://127.0.0.1:8000/mcp/<server_id>`, or the
public `https://.../mcp/<server_id>` once a reverse proxy is in front)
can call a read tool successfully.

## Phase 8 - Multi-server + `XmlRpcBackend` (Odoo <=18)

A second registry entry gets its own independent connector - same tools
(`ping`, `search_partners`, `get_partner`), same `Backend` contract, just
a different `backend:` value and transport:

```yaml
# config/servers.yaml
servers:
  odoo_a: { url: https://a.example.com, db: prod, version: 19, backend: json2 }
  odoo_b:
    url: https://b.example.com
    db: legacy_prod
    login: mcp-integration@example.com   # username/email - not secret, see below
    version: 17
    backend: xmlrpc
```

`XmlRpcBackend` (`src/odoo_mcp/backends/xmlrpc.py`) calls Odoo's legacy
XML-RPC API (`common.authenticate` then `execute_kw` on
`/xmlrpc/2/{common,object}`) instead of JSON-2's HTTP+bearer-key API.
Unlike JSON-2 (where the API key *is* the whole identity), XML-RPC needs
an explicit `db` + logged-in `login` for every call - `login` is a
username/email, not a secret, so it lives in `servers.yaml`; the
password half is still the same `ODOO_API_KEY` env var (Odoo accepts an
API key as the XML-RPC password since 14.0).

`gateway/http.py` now mounts **every** registry entry with a supported
`backend:` (`json2` *or* `xmlrpc`) at its own `/mcp/{server_id}` -
`/mcp/odoo_a` and `/mcp/odoo_b` run side by side, each only ever talking
to its own server/db. `gateway/stdio.py` (one server per process, picked
via `ODOO_MCP_SERVER`) now also works with either backend, via the new
`backends.backend_from_env` dispatcher.

Run the same tools against a real Odoo <=18 pilot directly (no MCP, like
`scripts/smoke_tool.py` for json2):

```bash
uv run --env-file .env python scripts/smoke_xmlrpc.py [name]
```

**Test / done when:** two independent connectors exist (one `json2`,
one `xmlrpc`), the HTTP gateway's `/healthz` lists both server ids, and
a client calling either `/mcp/{server_id}` gets that server's data with
no `server` parameter anywhere on the tools themselves. Automated
contract test (mocked, no real Odoo - proves `FakeBackend` /
`Json2Backend` / `XmlRpcBackend` expose the identical `search_read`
call/shape):

```bash
uv run pytest tests/test_backend_contract.py tests/test_xmlrpc_backend.py
```

## Phase 9 - Write tool + confirm + allowlist

The first write tool, `create_partner(name, email?, confirm=false)`,
creates one `res.partner`. It's gated two ways, matching the plan's
"Tool ghi + xác nhận":

1. **Allowlist (availability)** - `create_partner` is only *registered*
   at all (shows up in `tools/list`) on a server whose `write_tools`
   names it:

   ```yaml
   # config/servers.yaml
   servers:
     odoo_a:
       url: https://a.example.com
       version: 19
       backend: json2
       write_tools: [create_partner] # omit entirely for read-only servers
   ```

   No `config/servers.yaml` server (Option B, raw `ODOO_URL`/no
   registry)? Use the comma-separated `ODOO_MCP_WRITE_TOOLS` env var
   instead (see `.env.example`). Either way, the default is **no write
   tools** - every server/connector is read-only until explicitly opted
   in, and `gateway/http.py` applies this per `/mcp/{server_id}`
   independently (one server can allow writes, another not).

2. **Confirm (effect, per call)** - even when allowlisted, `confirm`
   defaults to `false`: the tool returns a preview of what *would* be
   written and never calls `Backend.create`. Only `confirm: true`
   actually writes:

   ```json
   // confirm omitted/false -> preview only, nothing written
   {"confirmed": false, "created": false, "preview": {"name": "Test Co"}}

   // confirm: true, key's user has create rights on res.partner
   {"confirmed": true, "created": true, "id": 42, "name": "Test Co"}
   ```

   Odoo's own ACLs for the API key's user still apply on every
   `confirm: true` call - insufficient permissions raise a normal
   `Json2Error`/`XmlRpcError` (never a silent no-op); this project never
   bypasses Odoo's access rights/record rules.

Manual smoke test against a real pilot (safe to run without
`--confirm` first):

```bash
uv run --env-file .env python scripts/smoke_write_tool.py "Test Co"
uv run --env-file .env python scripts/smoke_write_tool.py "Test Co" test@example.com --confirm
```

Automated tests (`FakeBackend` + mocks - no real Odoo):

```bash
uv run pytest tests/test_write_tools.py
```

**Test / done when:** without `confirm`, nothing is written to Odoo;
with `confirm` and sufficient rights, a real record is created and its
id returned; with `confirm` but insufficient rights, a clear Odoo error
comes back - never a silent no-op. `create_partner` never appears in
`tools/list` for a server that hasn't opted it into `write_tools`.

## Phase 10 - Ops checklist

No new code path here (beyond one combined smoke script below) - this
is the "vận hành nhẹ" checklist for keeping a running pilot stable
across the two things that actually change over time: rotating the API
key, and Odoo itself getting upgraded.

### Rotate the API key

The key is only ever read from `ODOO_API_KEY` (env/`.env`) - never
stored in `config/servers.yaml` or anywhere else in the repo (Phase 5),
so rotating it is a config change, never a code change:

1. In Odoo, as the same user: **Preferences -> Account Security -> API
   Keys** - create the new key first, *then* revoke/delete the old one
   (don't revoke first - that's a self-inflicted outage window).
2. Update `ODOO_API_KEY` in `.env` (local/stdio) and in whatever secret
   store feeds the container's `env_file:` (Phase 7 - `docker compose`
   does **not** hot-reload `.env`, see below).
3. Restart whatever process reads it:
   - stdio (Cursor/Claude Desktop): restart the client, or just the MCP
     server entry - it re-reads `.env` on next launch.
   - HTTP gateway: `docker compose restart gateway` (or re-run
     `uv run odoo-mcp-http` locally) - the key is read lazily per
     backend, but a stale process still has the old value cached in its
     env.
4. Run `scripts/smoke_after_upgrade.py` (below) once against the new
   key before trusting it with real client traffic - a bad rotation
   surfaces as a clear 401/ACL error, not a silent failure.

`xmlrpc` servers rotate the same way - the `login` username in
`servers.yaml` is not a secret and doesn't change; only `ODOO_API_KEY`
(used as the XML-RPC password) does.

### Smoke test after an Odoo upgrade

One combined script instead of re-running every earlier phase's smoke
test by hand: read contract (`search_partners` + `get_partner`) against
whichever backend the current `.env`/registry points at, an
installed-version eyeball check, and - if allowlisted - a preview-only
`create_partner` call to confirm the write-tool config still resolves.

```bash
uv run --env-file .env python scripts/smoke_after_upgrade.py
```

Run this right after any Odoo version/module upgrade on a pilot server,
and after every API key rotation above. Exit code 0 = read path (and
allowlisted write-tool config) still works; non-zero = a clear
connectivity/auth/ACL error to chase down before pointing real client
traffic at that server.

### Minimal log/metrics

Deliberately no external metrics stack for a single-instance pilot -
`gateway/http.py`'s `LoggingMiddleware` (Phase 6) already logs one line
per `/mcp/*` request (`mcp_request method=... path=... server_id=...
rpc=... tool=... status=... client=...`), and `RateLimitMiddleware`
logs `mcp_rate_limited ...` when a client gets throttled. Neither ever
logs headers, so the API key is never in the logs. In practice, that's
enough to answer the two questions that matter operationally: `grep
'status=5'`/`status=4` for error rate per server/tool, and
`grep mcp_rate_limited` for clients hitting the limit. `stdio.py` has no
equivalent (it's a local, single-client subprocess, not a shared
service) - nothing to add there.

### Deprecation note: prefer JSON-2 over XML/JSON-RPC

`backend: json2` (Phase 2) is the intended path for every Odoo 19+
pilot - Odoo has flagged the legacy XML-RPC/JSON-RPC APIs for eventual
removal in favor of JSON-2, so don't build new servers on
`backend: xmlrpc` just because it's available. `XmlRpcBackend` (Phase
8) exists **only** to reach Odoo <=18 servers that don't have JSON-2 at
all - keep it for those, and move a server to `json2` as soon as it's
upgraded to 19+. Check the official Odoo docs for the exact removal
version before assuming XML-RPC will keep working indefinitely on a
future upgrade.

## Phase 11 - Generic tools (model-agnostic)

The fixed, business-shaped tools from Phase 4/9 (`search_partners`,
`get_partner`, `create_partner`) are gone from `tools/list` on every
gateway (`stdio.py` and `http.py`). In their place, `core.mcp_server
.build_mcp_server` now registers generic, model-agnostic tools an AI
caller drives itself:

| Tool | Always registered? | Behavior |
|---|---|---|
| `describe_model(model)` | yes | `fields_get` (string/type/required/relation/selection). |
| `search_read(model, domain, fields, limit=20, offset=0)` | yes | `limit` capped at 100. `fields` must be passed explicitly (avoids pulling e.g. `image_1920`). |
| `create(model, values, confirm=false)` | only if `write_tools` includes `create` | |
| `write(model, ids, values, confirm=false)` | only if `write_tools` includes `write` | `ids` must be non-empty, at most 100. |

`write_tools` (Phase 9, per-server, `config/servers.yaml`) is the only
config allowlist left - which write tools exist at all on this server.
`WRITE_TOOL_NAMES` is `{create, write}` (an unknown name is a startup
`ValueError`, not a silent no-op). Model/field access itself was a
second, `allowed_models` config allowlist from Phase 11 through Phase
15 - Phase 16 (below) removes it in favor of Odoo's own access rights.

```yaml
# config/servers.yaml
servers:
  odoo_a:
    url: https://a.example.com
    version: 19
    backend: json2
    write_tools: [create, write]      # omit entirely for read-only servers
```

`core.tools.search_partners` / `get_partner` / `create_partner` (the
Phase 4/9 Python functions) still exist, kept only for the direct-call
smoke scripts (`scripts/smoke_tool.py`, `scripts/smoke_write_tool.py`,
`scripts/smoke_after_upgrade.py`) - they are no longer registered on
any `FastMCP`.

Automated tests (`FakeBackend` - no real Odoo):

```bash
uv run pytest tests/test_generic_tools.py tests/test_write_tools.py tests/test_registry.py
```

**Test / done when:** `confirm=false` never writes; `confirm=true`
writes exactly `values`; `search_partners`/`get_partner`/
`create_partner` no longer appear in any server's `tools/list`.

## Phase 12 - Consent trên Odoo (OAuth 2.1 + vault)

The HTTP gateway (Phase 6) now defaults to per-user OAuth instead of one
shared `ODOO_API_KEY`: a Claude connection redirects the browser into a
thin Odoo addon (`addon/odoo_mcp`) where the *already-logged-in* Odoo
user clicks Allow, Odoo mints a real `res.users.apikeys` key named
`Claude MCP` scoped to that user, and the gateway stores it encrypted in
a local SQLite vault keyed by a Claude-issued bearer token. Every
`/mcp/{server_id}` tool call after that runs with *that* user's own
Odoo permissions - never a shared key.

New pieces:

- `addon/odoo_mcp` - install on the Odoo 19 pilot: consent page at
  `/odoo_mcp/authorize`, one-time code exchange at `/odoo_mcp/token`,
  and Settings > General Settings > MCP Gateway for the channel secret
  + allowed gateway redirect URIs.
- `src/odoo_mcp/auth/` - the gateway's OAuth 2.1 authorization server
  (PKCE, dynamic client registration, protected-resource metadata) on
  top of the `mcp` SDK's `mcp.server.auth.*` framework, plus the
  Fernet-encrypted SQLite vault (`auth/vault.py`).

Env vars (see `.env.example` for the full list):

- `ODOO_MCP_PUBLIC_URL` - this gateway's public https URL (the OAuth
  issuer Claude sees). Required unless `ODOO_MCP_ALLOW_SHARED_KEY=1`.
- `ODOO_MCP_VAULT_KEY` - long random secret encrypting API keys at rest
  in `config/vault.sqlite3` (git-ignored). Required unless
  `ODOO_MCP_ALLOW_SHARED_KEY=1`.
- `ODOO_MCP_CHANNEL_SECRET_<SERVER_ID>` (e.g.
  `ODOO_MCP_CHANNEL_SECRET_ODOO_A`) - must match that server's Settings
  > MCP Gateway > "MCP Channel Secret" in Odoo.
- `ODOO_MCP_ALLOW_SHARED_KEY=1` - opts a whole gateway process back into
  the Phase 2-11 one-shared-`ODOO_API_KEY` model, no OAuth, no addon
  required. Kept for `scripts/smoke_mcp_http.py` and existing
  single-tenant deployments; leave unset (OAuth on) for new ones.

Automated tests (no real Odoo, no network - metadata endpoints,
`/authorize` redirect target, the odoo-callback -> vault -> redirect
to Claude path with a mocked token exchange, `/mcp/{id}` 401 without a
token, and a tool call resolving the calling user's own vault-stored
key):

```bash
uv run pytest tests/test_vault.py tests/test_oauth_provider.py tests/test_http_oauth.py
```

Manual checklist on a real Odoo 19 pilot (not automatable - requires
installing `addon/odoo_mcp` and a live browser session):

1. Install `addon/odoo_mcp`, set the channel secret + this gateway's
   redirect URI in Settings > MCP Gateway, and set the matching
   `ODOO_MCP_CHANNEL_SECRET_<SERVER_ID>` / `ODOO_MCP_PUBLIC_URL` /
   `ODOO_MCP_VAULT_KEY` on the gateway.
2. From a browser with no Odoo session, start a Claude connection to
   `/mcp/<server_id>` - confirm it lands on `/web/login`, then returns
   to the consent page after logging in (not a broken redirect loop).
3. Click **Allow** - confirm **Account Security** on that Odoo user now
   has an API key named `Claude MCP`, and that Claude's next tool call
   succeeds and reflects that user's own Odoo permissions (try a user
   with restricted read access on `res.partner` and confirm the tool
   call is scoped accordingly).
4. Click **Deny** on a fresh connection attempt - confirm no new
   `Claude MCP` key is created and Claude sees `error=access_denied`.
5. Reuse the same one-time `code` a second time against
   `/odoo_mcp/token` (e.g. replay the request) - confirm Odoo returns
   `400`/`invalid_grant`, not a second successful redemption.

### Self-host web UI clients (e.g. LibreChat)

The OAuth 2.1 flow above (PKCE, dynamic client registration,
protected-resource metadata) is generic - any MCP client that speaks
OAuth can connect to `/mcp/{server_id}`, not only Claude.ai. This
gateway is never patched for a specific client; only
`ODOO_MCP_OAUTH_REDIRECT_HOSTS` needs that client's hostname added.
See [`blueprint-librechat.md`](blueprint-librechat.md) for the design
and [`deploy/librechat/`](deploy/librechat/) for a runnable
docker-compose stack (LibreChat + its own MongoDB/Meilisearch,
separate from this repo's `docker-compose.yml`).

## Phase 13 - Aggregate tools (`search_count`/`read_group`/`name_search`)

Before this phase, the only read tool was `search_read` - a "how many
partners?" question could only be answered by paging rows and counting
them in the AI's own context (many round trips, and every earlier page
resent on every later turn). `core.mcp_server.build_mcp_server` now also
registers three small, model-agnostic tools that answer in one small
result instead:

| Tool | Behavior |
|---|---|
| `search_count(model, domain)` | Odoo `search_count` - one integer, never rows. |
| `read_group(model, fields, groupby, domain, limit=20, offset=0)` | Odoo `read_group` - one row per group (e.g. partners by country, sum of `amount_total` per state), not one row per record. `limit`/`offset` page *groups*, capped at 100 like `search_read`. |
| `name_search(model, name, domain, operator, limit=20)` | Odoo `name_search` - `(id, display_name)` pairs for a name substring, e.g. resolving a many2one label without a full `search_read`. Capped at 100. |

`search_read`'s own docstring now points the AI caller at these three
for "how many"/"total by"/"find the id of" instead of paging rows to
compute the answer itself.

`Backend.search_count`/`read_group`/`name_search` are implemented on
all three backends (`FakeBackend`, `Json2Backend`, `XmlRpcBackend`) -
JSON-2 and XML-RPC both expose Odoo's own methods of the same name on
every model (confirmed against the Odoo 19 source: `search_count`,
`read_group` (documented "deprecated" but still the stable public
entry point both transports share - the newer `_read_group` is
private, i.e. not reachable over JSON-2/XML-RPC), and `name_search`).

Automated tests (`FakeBackend` + mocked json2/xmlrpc - no real Odoo):

```bash
uv run pytest tests/test_generic_tools.py tests/test_backend_contract.py
```

**Test / done when:** `search_count` returns a plain integer;
`read_group` returns one dict per group, not per record; `name_search`
returns `(id, display_name)` pairs; all three cap their `limit` at 100
the same way `search_read` does.

## Phase 16 - Model access moves onto Odoo's own rights

Phase 11's `allowed_models` config allowlist (per-server, per-model
read/write field lists in `servers.yaml`) is gone, along with the
`list_models()` tool that only ever read it. Model/field access for
every generic tool (`describe_model`/`search_read`/`search_count`/
`read_group`/`name_search`/`create`/`write`) is now entirely the
API-key user's own Odoo permissions - `ir.model.access`, `ir.rule`
record rules, and field-level `groups`, enforced by Odoo itself on
every call, not by a config lookup on the gateway.

`list_models()` is removed rather than reimplemented against Odoo:
"which models can this user read" has to be computed inside Odoo
(`env[model].has_access("read")` over the whole registry), and
`check_access`/`has_access` are `@api.private` - not reachable over
JSON-2/XML-RPC. A caller who wants to see installed models can
`search_read` `ir.model` like any other model (same `fields`
requirement, same `limit` cap, same ACL as everywhere else), subject
to that user's own read access on it.

`write_tools` (Phase 9) is unchanged - still the one config allowlist
left, deciding whether `create`/`write` are registered at all.

Automated tests (`FakeBackend` - no real Odoo):

```bash
uv run pytest tests/test_generic_tools.py tests/test_registry.py tests/test_http_oauth.py
```

**Test / done when:** every generic tool forwards `model`/`domain`/
`fields`/`values` straight to the `Backend` with no local allowlist
check; `search_read` without `fields` still raises before calling the
backend; `confirm=false` never writes; `list_models` is no longer
registered on any `FastMCP`; `ServerConfig`/`load_registry` no longer
know about `allowed_models` (a leftover key in an existing
`servers.yaml` is ignored, not an error).
