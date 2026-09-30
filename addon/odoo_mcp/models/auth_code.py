# Part of odoo-mcp. See LICENSE file for full copyright and licensing details.
"""One-time consent code: bridges `/odoo_mcp/authorize` (a real, logged-in
browser session) and `/odoo_mcp/token` (the MCP gateway, server-to-server -
see `controllers/main.py`) without ever putting the plaintext Odoo API key
in the browser.

Flow (see blueprint-v3.md, Phase 12):

1. User clicks "allow" on `/odoo_mcp/authorize` -> `_issue()` generates a
   fresh `res.users.apikeys` key *for that user*, encrypts it with a Fernet
   key derived from the gateway's channel secret, and stores it here under
   a random one-time `code`.
2. The gateway calls `/odoo_mcp/token` with that `code` and the same
   channel secret -> `_redeem()` decrypts and returns the plaintext key
   once, then deletes the row so the code can never be redeemed twice.

No group has any access right on this model (see
`security/ir.model.access.csv`) - only the two `sudo()` calls below ever
touch it, and a row lives at most `CODE_TTL_MINUTES` either because it was
redeemed or because `_gc_expired` (the framework-wide autovacuum cron)
swept it.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken

from odoo import api, fields, models
from odoo.exceptions import ValidationError

CODE_TTL_MINUTES = 2
#: Cap for a non-admin user - the real cap is always the lowest of this and
#: the user's own role `api_key_duration` (see `_expiration_within_role`,
#: mirroring `res.users.apikeys._check_expiration_date` in Odoo core).
DEFAULT_KEY_DURATION_DAYS = 30
KEY_NAME = "Claude MCP"


def _fernet_from_secret(secret: str) -> Fernet:
    """Derive a Fernet key deterministically from the channel secret
    (`odoo_mcp.channel_secret`, see `ResConfigSettings`) - no separate
    encryption key needs to be generated/stored; decrypting always just
    requires the same secret the gateway presents at `/odoo_mcp/token`."""
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _expiration_within_role(env) -> datetime.datetime:
    """Mirror `res.users.apikeys._check_expiration_date` (Odoo core,
    `odoo/addons/base/models/res_users.py`, 19.0): admins (`env.is_system()`)
    can hold a long-lived key, everyone else is capped at their own role's
    `api_key_duration`. Always returns a value `_generate` will accept."""
    if env.is_system():
        return datetime.datetime.now() + datetime.timedelta(days=DEFAULT_KEY_DURATION_DAYS)
    max_duration = max(
        (group.api_key_duration for group in env.user.all_group_ids), default=0.0
    ) or 1.0
    return datetime.datetime.now() + datetime.timedelta(
        days=min(DEFAULT_KEY_DURATION_DAYS, max_duration)
    )


class OdooMcpAuthCode(models.Model):
    _name = "odoo.mcp.auth.code"
    _description = "Odoo MCP - one-time consent auth code"

    code = fields.Char(required=True, index=True)
    user_id = fields.Many2one("res.users", required=True, ondelete="cascade")
    login = fields.Char(required=True)
    redirect_uri = fields.Char(required=True)
    encrypted_api_key = fields.Char(required=True)
    expires_at = fields.Datetime(required=True)

    @api.model
    def _issue(self, *, redirect_uri: str, channel_secret: str) -> str:
        """Called from `/odoo_mcp/authorize` (POST), in the consenting
        user's own non-sudo environment - `_generate` below therefore runs
        as `self.env.user`, so the new key is genuinely theirs. Returns the
        one-time code; the plaintext API key is never returned to the
        browser."""
        user = self.env.user
        expiration = _expiration_within_role(self.env)
        api_key = self.env["res.users.apikeys"]._generate(None, KEY_NAME, expiration)

        code = secrets.token_urlsafe(32)
        self.sudo().create(
            {
                "code": code,
                "user_id": user.id,
                "login": user.login,
                "redirect_uri": redirect_uri,
                "encrypted_api_key": _fernet_from_secret(channel_secret)
                .encrypt(api_key.encode())
                .decode(),
                "expires_at": fields.Datetime.now()
                + datetime.timedelta(minutes=CODE_TTL_MINUTES),
            }
        )
        return code

    @api.model
    def _redeem(self, *, code: str, redirect_uri: str, channel_secret: str) -> dict:
        """Called from `/odoo_mcp/token` (the gateway, server-to-server,
        `auth='none'` - see `controllers/main.py`). Returns
        `{api_key, login, uid}` and deletes the row so the code can never
        be redeemed twice. Raises `ValidationError` (mapped to an
        `invalid_grant` JSON error by the controller) for anything wrong:
        unknown/already-used code, `redirect_uri` mismatch, expiry, or a
        key that was encrypted with a different channel secret."""
        record = self.sudo().search([("code", "=", code)], limit=1)
        if not record or record.redirect_uri != redirect_uri:
            raise ValidationError("Invalid or already-used auth code.")
        if record.expires_at < fields.Datetime.now():
            record.unlink()
            raise ValidationError("Auth code expired.")
        try:
            api_key = (
                _fernet_from_secret(channel_secret)
                .decrypt(record.encrypted_api_key.encode())
                .decode()
            )
        except InvalidToken:
            raise ValidationError(
                "Auth code could not be decrypted - check the channel secret."
            )
        result = {"api_key": api_key, "login": record.login, "uid": record.user_id.id}
        record.unlink()
        return result

    @api.autovacuum
    def _gc_expired(self):
        self.sudo().search([("expires_at", "<", fields.Datetime.now())]).unlink()
