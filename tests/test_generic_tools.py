"""Phase 11/16 tests: the generic, model-agnostic tools registered by
`core.mcp_server.build_mcp_server` (`describe_model`, `search_read`,
`search_count`, `read_group`, `name_search`, `create`, `write`) plus
the one remaining config allowlist (`write_tools`, which tools exist
at all).

No network, no real Odoo - exercised directly against `FakeBackend`
(`res.partner`, static `id`/`name`/`email` schema). Phase 16 removed
the `allowed_models` model/field allowlist (model/field access is now
the API-key user's own Odoo rights, enforced by Odoo itself - not
testable against `FakeBackend`), so these tests instead check that
every tool forwards `model`/`domain`/`fields`/`values` straight to the
`Backend` with no local allowlist check: `confirm=false` never writes;
`confirm=true` writes exactly `values`; `search_read` without `fields`
still errors before calling the backend; `limit` is still capped at
100 everywhere; `list_models` is no longer registered.
"""

from __future__ import annotations

import pytest

from odoo_mcp.backends.fake import FakeBackend
from odoo_mcp.core.mcp_server import build_mcp_server


def _tool(mcp, name: str):
    tool = mcp._tool_manager.get_tool(name)
    assert tool is not None, f"tool '{name}' not registered"
    return tool


# --------------------------------------------------------------------------
# list_models removed
# --------------------------------------------------------------------------


def test_list_models_no_longer_registered() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    assert mcp._tool_manager.get_tool("list_models") is None


# --------------------------------------------------------------------------
# describe_model
# --------------------------------------------------------------------------


def test_describe_model_returns_every_field() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    result = _tool(mcp, "describe_model").fn(model="res.partner")

    assert set(result) == {"id", "name", "email"}
    assert result["name"] == {"string": "Name", "type": "char"}


def test_describe_model_filters_to_requested_fields() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    result = _tool(mcp, "describe_model").fn(model="res.partner", fields=["name"])

    assert set(result) == {"name"}
    assert result["name"] == {"string": "Name", "type": "char"}


def test_describe_model_raises_when_no_requested_field_exists() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    with pytest.raises(ValueError, match="not_a_field"):
        _tool(mcp, "describe_model").fn(model="res.partner", fields=["not_a_field"])


# --------------------------------------------------------------------------
# search_read
# --------------------------------------------------------------------------


def test_search_read_requires_explicit_fields() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    with pytest.raises(ValueError, match="must be passed explicitly"):
        _tool(mcp, "search_read").fn(model="res.partner")


def test_search_read_with_fields_returns_projected_records() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    result = _tool(mcp, "search_read").fn(model="res.partner", fields=["id", "name"], limit=1)

    assert result == [{"id": 1, "name": "Azure Interior"}]


def test_search_read_forwards_domain_to_backend() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    result = _tool(mcp, "search_read").fn(
        model="res.partner", domain=[("name", "ilike", "Azure")], fields=["id", "name"]
    )

    assert result == [{"id": 1, "name": "Azure Interior"}]


def test_search_read_caps_limit_at_max() -> None:
    calls: dict[str, object] = {}

    class _Recording(FakeBackend):
        def search_read(self, model, domain=None, fields=None, limit=None, offset=0):
            calls["limit"] = limit
            return super().search_read(model, domain=domain, fields=fields, limit=limit, offset=offset)

    mcp = build_mcp_server("test", _Recording)

    _tool(mcp, "search_read").fn(model="res.partner", fields=["id"], limit=1000)

    assert calls["limit"] == 100


# --------------------------------------------------------------------------
# search_count
# --------------------------------------------------------------------------


def test_search_count_returns_int() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    result = _tool(mcp, "search_count").fn(model="res.partner")

    assert result == 4


def test_search_count_forwards_domain_to_backend() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    result = _tool(mcp, "search_count").fn(model="res.partner", domain=[("name", "ilike", "Azure")])

    assert result == 1


# --------------------------------------------------------------------------
# read_group
# --------------------------------------------------------------------------


def test_read_group_returns_one_row_per_group() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    result = _tool(mcp, "read_group").fn(
        model="res.partner",
        domain=[("name", "ilike", "Azure")],
        fields=["name"],
        groupby=["name"],
    )

    assert result == [{"name": "Azure Interior", "__count": 1}]


def test_read_group_caps_limit_at_max() -> None:
    calls: dict[str, object] = {}

    class _Recording(FakeBackend):
        def read_group(self, model, domain, fields, groupby, limit=None, offset=0):
            calls["limit"] = limit
            return super().read_group(
                model, domain=domain, fields=fields, groupby=groupby, limit=limit, offset=offset
            )

    mcp = build_mcp_server("test", _Recording)

    _tool(mcp, "read_group").fn(model="res.partner", fields=["name"], groupby=["name"], limit=1000)

    assert calls["limit"] == 100


# --------------------------------------------------------------------------
# name_search
# --------------------------------------------------------------------------


def test_name_search_returns_id_name_pairs() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    result = _tool(mcp, "name_search").fn(model="res.partner", name="Azure")

    assert result == [(1, "Azure Interior")]


def test_name_search_caps_limit_at_max() -> None:
    calls: dict[str, object] = {}

    class _Recording(FakeBackend):
        def name_search(self, model, name="", domain=None, operator="ilike", limit=100):
            calls["limit"] = limit
            return super().name_search(model, name=name, domain=domain, operator=operator, limit=limit)

    mcp = build_mcp_server("test", _Recording)

    _tool(mcp, "name_search").fn(model="res.partner", limit=1000)

    assert calls["limit"] == 100


# --------------------------------------------------------------------------
# create / write tool availability
# --------------------------------------------------------------------------


def test_create_and_write_not_registered_without_write_tools() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    assert mcp._tool_manager.get_tool("create") is None
    assert mcp._tool_manager.get_tool("write") is None


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def test_create_without_confirm_does_not_write() -> None:
    backend = FakeBackend()
    mcp = build_mcp_server("test", lambda: backend, write_tools={"create"})
    before = backend.search_read("res.partner")

    result = _tool(mcp, "create").fn(model="res.partner", values={"name": "New Co"})

    assert result == {"confirmed": False, "created": False, "preview": {"name": "New Co"}}
    assert backend.search_read("res.partner") == before


def test_create_with_confirm_writes_values() -> None:
    backend = FakeBackend()
    mcp = build_mcp_server("test", lambda: backend, write_tools={"create"})

    result = _tool(mcp, "create").fn(model="res.partner", values={"name": "New Co"}, confirm=True)

    assert result["confirmed"] is True
    assert result["created"] is True
    assert isinstance(result["id"], int)


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------


def test_write_without_confirm_does_not_write() -> None:
    backend = FakeBackend()
    mcp = build_mcp_server("test", lambda: backend, write_tools={"write"})
    before = backend.search_read("res.partner")

    result = _tool(mcp, "write").fn(model="res.partner", ids=[1], values={"name": "Renamed"})

    assert result == {
        "confirmed": False,
        "written": False,
        "ids": [1],
        "preview": {"name": "Renamed"},
    }
    assert backend.search_read("res.partner") == before


def test_write_with_confirm_updates_record() -> None:
    backend = FakeBackend()
    mcp = build_mcp_server("test", lambda: backend, write_tools={"write"})

    result = _tool(mcp, "write").fn(model="res.partner", ids=[1], values={"name": "Renamed"}, confirm=True)

    assert result["confirmed"] is True
    assert result["written"] is True
    updated = backend.read("res.partner", [1])
    assert updated[0]["name"] == "Renamed"


def test_write_rejects_empty_ids() -> None:
    mcp = build_mcp_server("test", FakeBackend, write_tools={"write"})

    with pytest.raises(ValueError, match="must not be empty"):
        _tool(mcp, "write").fn(model="res.partner", ids=[], values={"name": "x"})


def test_write_rejects_more_than_max_ids() -> None:
    mcp = build_mcp_server("test", FakeBackend, write_tools={"write"})

    with pytest.raises(ValueError, match="more than 100 ids"):
        _tool(mcp, "write").fn(model="res.partner", ids=list(range(101)), values={"name": "x"})
