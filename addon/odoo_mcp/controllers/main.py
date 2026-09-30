# Part of odoo-mcp. See LICENSE file for full copyright and licensing details.
"""Phase 12 controllers - the addon side of the consent flow described in
blueprint-v3.md: the gateway redirects the browser here after the user
already has (or gets) an Odoo session; the user sees who's asking, clicks
allow/deny, and only on allow does a fresh `res.users.apikeys` key ever get
created (see `models/auth_code.py` for the code<->key bookkeeping).

Nothing here talks MCP or OAuth - the gateway (`src/odoo_mcp/auth/`) is the
only piece that speaks that side of the flow; this addon only ever answers
"is this user, do they say yes, here is their key" for one `redirect_uri`
at a time, checked against the allowlist configured in Settings (see
`models/res_config_settings.py`).

`/odoo_mcp/authorize` is `auth='user'` alone (no explicit login handling
here): an anonymous browser hitting it is sent to `/web/login?redirect=...`
first by Odoo's own dispatcher (see `web.Home.web_client` /
`SessionExpiredException` for the equivalent core behaviour) - by the time
either method below runs there is always a real `request.env.user`. This
mirrors `mail_plugin.controllers.authenticate.Authenticate.auth`
(`/mail_plugin/auth`), the closest core example of a third-party-app
consent screen.
"""

from __future__ import annotations

import hmac
import logging

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

_logger = logging.getLogger(__name__)


def _allowed_redirect_uris(env) -> set[str]:
    raw = env["ir.config_parameter"].sudo().get_param("odoo_mcp.redirect_uris", "") or ""
    return {line.strip() for line in raw.splitlines() if line.strip()}


def _channel_secret(env) -> str:
    return env["ir.config_parameter"].sudo().get_param("odoo_mcp.channel_secret") or ""


class OdooMcpConsent(http.Controller):
    """`/odoo_mcp/authorize` - shown to (and confirmed by) a real user."""

    @http.route("/odoo_mcp/authorize", type="http", auth="user", methods=["GET"], website=True)
    def authorize(self, redirect_uri=None, state="", **kw):
        """Renders the consent page."""
        if not redirect_uri or redirect_uri not in _allowed_redirect_uris(request.env):
            return request.render(
                "odoo_mcp.consent_error",
                {"message": "Unknown or unconfigured gateway redirect_uri."},
            )
        return request.render(
            "odoo_mcp.consent_page",
            {"user": request.env.user, "redirect_uri": redirect_uri, "state": state},
        )

    @http.route("/odoo_mcp/authorize", type="http", auth="user", methods=["POST"])
    def authorize_confirm(self, redirect_uri=None, state="", allow=None, **kw):
        if not redirect_uri or redirect_uri not in _allowed_redirect_uris(request.env):
            return request.render(
                "odoo_mcp.consent_error",
                {"message": "Unknown or unconfigured gateway redirect_uri."},
            )
        if not allow:
            return request.redirect(f"{redirect_uri}?error=access_denied&state={state}", local=False)

        channel_secret = _channel_secret(request.env)
        if not channel_secret:
            return request.render(
                "odoo_mcp.consent_error",
                {
                    "message": (
                        "This server has no odoo_mcp.channel_secret configured "
                        "yet - ask an administrator (Settings > MCP Gateway)."
                    )
                },
            )

        try:
            code = request.env["odoo.mcp.auth.code"]._issue(
                redirect_uri=redirect_uri, channel_secret=channel_secret
            )
        except ValidationError as exc:
            return request.render("odoo_mcp.consent_error", {"message": str(exc)})

        return request.redirect(f"{redirect_uri}?code={code}&state={state}", local=False)


class OdooMcpToken(http.Controller):
    """`/odoo_mcp/token` - server-to-server only, never touched by a
    browser. Identity comes entirely from the one-time `code` plus the
    shared `channel_secret` - no session/cookie involved (`auth='none'`),
    matching what `models.auth_code.OdooMcpAuthCode._redeem` expects."""

    @http.route("/odoo_mcp/token", type="jsonrpc", auth="none", methods=["POST"])
    def token(self, code=None, redirect_uri=None, secret=None, **kw):
        configured_secret = _channel_secret(request.env)
        if not configured_secret or not secret or not hmac.compare_digest(secret, configured_secret):
            _logger.warning("odoo_mcp token exchange rejected: bad channel secret")
            return {"error": "invalid_secret"}
        if not code or not redirect_uri:
            return {"error": "invalid_request"}

        try:
            return request.env["odoo.mcp.auth.code"]._redeem(
                code=code, redirect_uri=redirect_uri, channel_secret=configured_secret
            )
        except ValidationError as exc:
            return {"error": "invalid_grant", "error_description": str(exc)}
