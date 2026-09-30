# Part of odoo-mcp. See LICENSE file for full copyright and licensing details.
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    odoo_mcp_channel_secret = fields.Char(
        string="MCP Channel Secret",
        config_parameter="odoo_mcp.channel_secret",
        help=(
            "Shared secret the MCP gateway presents at /odoo_mcp/token when "
            "redeeming a consent auth code. Generate one per gateway "
            "deployment and configure the exact same value on the gateway "
            "side (ODOO_MCP_CHANNEL_SECRET_<server_id>)."
        ),
    )
    odoo_mcp_redirect_uris = fields.Char(
        string="MCP Allowed Redirect URIs",
        config_parameter="odoo_mcp.redirect_uris",
        help=(
            "One gateway callback URL per line. /odoo_mcp/authorize only "
            "accepts a redirect_uri that matches one of these exactly."
        ),
    )
