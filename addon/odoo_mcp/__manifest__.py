# Part of odoo-mcp. See LICENSE file for full copyright and licensing details.
{
    "name": "Odoo MCP Consent",
    "version": "19.0.1.0.0",
    "category": "Extra Tools",
    "summary": "Let a signed-in user grant an MCP gateway API access with their own permissions",
    "description": """
Thin consent layer for the Odoo MCP gateway (see blueprint-v3.md, Phase 12
in the odoo-mcp repo): lets an already-logged-in user approve a gateway
connector with one click. On approval, generates a personal API key
(res.users.apikeys) and hands it to the gateway through a short-lived,
single-use code - never through the browser.

No MCP tools live here. This addon only ever answers "who is this user,
did they say yes, here is their key" for one configured gateway
redirect_uri at a time (see Settings > General Settings > MCP Gateway).
""",
    "depends": ["base", "base_setup", "web"],
    "data": [
        "security/ir.model.access.csv",
        "views/templates.xml",
        "views/res_config_settings_views.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
