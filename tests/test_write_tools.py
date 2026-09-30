"""Phase 9 tests: `create_partner` (the pure `core.tools` function, kept
for `scripts/smoke_tool.py`) + `write_tools_from_env`. Phase 11 renamed
the actual registered write tools to generic `create`/`write` - see
`tests/test_generic_tools.py` for those.

No network, no real Odoo:
- `core_tools.create_partner` is exercised against `FakeBackend` (in
  memory) plus a mock backend for the "insufficient permission" path.
- `build_mcp_server`'s write-tool allowlist is exercised directly (which
  tools it actually registers), matching the plan's "Test / xong khi"
  criteria: unknown name -> `ValueError` at startup; allowlisted -> the
  tool is registered; not allowlisted -> it is not.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from odoo_mcp.backends import write_tools_from_env
from odoo_mcp.backends.fake import FakeBackend
from odoo_mcp.core import tools as core_tools
from odoo_mcp.core.backend import Backend
from odoo_mcp.core.mcp_server import WRITE_TOOL_NAMES, build_mcp_server

_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "config" / "servers.example.yaml"


def _registered_tool_names(mcp) -> set[str]:
    return {tool.name for tool in mcp._tool_manager.list_tools()}


def test_create_partner_without_confirm_does_not_write() -> None:
    backend = FakeBackend()
    before = backend.search_read("res.partner")

    result = core_tools.create_partner(backend, name="New Co", email="new@example.com")

    assert result == {
        "confirmed": False,
        "created": False,
        "preview": {"name": "New Co", "email": "new@example.com"},
    }
    assert backend.search_read("res.partner") == before  # nothing written


def test_create_partner_with_confirm_writes_and_returns_id() -> None:
    backend = FakeBackend()
    before_count = len(backend.search_read("res.partner"))

    result = core_tools.create_partner(
        backend, name="New Co", email="new@example.com", confirm=True
    )

    assert result["confirmed"] is True
    assert result["created"] is True
    assert isinstance(result["id"], int)
    after = backend.search_read("res.partner")
    assert len(after) == before_count + 1
    assert {"id": result["id"], "name": "New Co", "email": "new@example.com"} in after


def test_create_partner_confirm_propagates_backend_error() -> None:
    """Insufficient Odoo permissions surface as a normal backend error,
    never a silent no-op - modeled here as `Backend.create` raising."""
    backend = MagicMock(spec=Backend)
    backend.create.side_effect = RuntimeError("Odoo: Access Denied")

    with pytest.raises(RuntimeError, match="Access Denied"):
        core_tools.create_partner(backend, name="New Co", confirm=True)


def test_build_mcp_server_write_tools_not_registered_by_default() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    names = _registered_tool_names(mcp)
    assert "create" not in names
    assert "write" not in names
    assert {
        "ping",
        "list_models",
        "describe_model",
        "search_read",
        "search_count",
        "read_group",
        "name_search",
    } <= names


def test_build_mcp_server_write_tools_registered_when_allowlisted() -> None:
    mcp = build_mcp_server("test", FakeBackend, write_tools={"create", "write"})

    assert {"create", "write"} <= _registered_tool_names(mcp)


def test_build_mcp_server_rejects_unknown_write_tool_name() -> None:
    with pytest.raises(ValueError, match="Unknown write tool"):
        build_mcp_server("test", FakeBackend, write_tools={"delete_everything"})


def test_build_mcp_server_rejects_old_create_partner_name() -> None:
    """Phase 11 renamed the write tools - the old Phase 9 name must now
    fail loudly instead of silently doing nothing."""
    with pytest.raises(ValueError, match="Unknown write tool"):
        build_mcp_server("test", FakeBackend, write_tools={"create_partner"})


def test_write_tool_names_is_create_and_write() -> None:
    assert WRITE_TOOL_NAMES == frozenset({"create", "write"})


def test_write_tools_from_env_uses_registry_when_server_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "servers.yaml"
    config.write_text(
        "servers:\n  odoo_a:\n    url: https://s.example.com\n"
        "    version: 19\n    backend: json2\n"
        "    write_tools: [create_partner]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ODOO_MCP_SERVER", "odoo_a")
    monkeypatch.setenv("ODOO_MCP_CONFIG", str(config))

    assert write_tools_from_env(load_dotenv_file=False) == frozenset({"create_partner"})


def test_write_tools_from_env_registry_defaults_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODOO_MCP_SERVER", "odoo_a")
    monkeypatch.setenv("ODOO_MCP_CONFIG", str(_EXAMPLE_PATH))

    assert write_tools_from_env(load_dotenv_file=False) == frozenset()


def test_write_tools_from_env_falls_back_to_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ODOO_MCP_SERVER", raising=False)
    monkeypatch.setenv("ODOO_MCP_WRITE_TOOLS", "create_partner, other_tool")

    assert write_tools_from_env(load_dotenv_file=False) == frozenset(
        {"create_partner", "other_tool"}
    )


def test_write_tools_from_env_no_server_no_env_var_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ODOO_MCP_SERVER", raising=False)
    monkeypatch.delenv("ODOO_MCP_WRITE_TOOLS", raising=False)

    assert write_tools_from_env(load_dotenv_file=False) == frozenset()
