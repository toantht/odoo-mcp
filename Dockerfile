# Phase 7: package the HTTP gateway (`gateway/http.py`) as a container.
#
# The stdio gateway (`gateway/stdio.py`) is meant to run as a local
# subprocess launched by an MCP client (Cursor/Claude Desktop) - it is
# not what this image runs. This image only serves `odoo-mcp-http`,
# meant to sit behind a reverse proxy that terminates HTTPS (see
# docker-compose.yml + README "Phase 7").

FROM python:3.12-slim AS base

# Match the uv version pinned by pyproject.toml's build-system
# (`uv_build>=0.12.5,<0.13.0`) so the project builds the same way it
# does locally.
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# Install dependencies first, from the lockfile only, so this layer is
# cached across rebuilds that only touch application code.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now add the actual project and install it too.
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# `config/servers.yaml` (Phase 5, git-ignored - see .gitignore) is
# expected to be bind-mounted/volume-mounted at runtime; ship the
# example only so the image still has *something* to fall back on for
# `ODOO_MCP_CONFIG` if the caller forgets to mount a real one (the
# gateway will just fail per-server-config, not at startup - see
# gateway/http.py).
COPY config/servers.example.yaml ./config/servers.example.yaml

# Container-friendly defaults: bind every interface (a laptop-only
# 127.0.0.1 default is useless inside a container), reverse proxy or
# `docker run -p` handles what's actually exposed.
ENV ODOO_MCP_HTTP_HOST=0.0.0.0 \
    ODOO_MCP_HTTP_PORT=8000

EXPOSE 8000

CMD ["odoo-mcp-http"]
