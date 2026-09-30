"""Phase 11 tests: the generic, model-agnostic tools registered by
`core.mcp_server.build_mcp_server` (`list_models`, `describe_model`,
`search_read`, `create`, `write`) plus the two allowlists that gate
them (`allowed_models` for models/fields, `write_tools` for tool
availability).

No network, no real Odoo - exercised directly against `FakeBackend`
(`res.partner`, static `id`/`name`/`email` schema), matching the plan's
"Test / xong khi" criteria: unknown model -> clear error before the
backend is ever called; read/write/domain field outside the allowlist
-> clear error; `confirm=false` never writes; `confirm=true` writes
exactly the allowlisted fields.
"""

from __future__ import annotations

import pytest

from odoo_mcp.backends.fake import FakeBackend
from odoo_mcp.config.registry import ModelAccess
from odoo_mcp.core.mcp_server import build_mcp_server

_PARTNER_ALL = {"res.partner": ModelAccess(read="all")}
_PARTNER_READ_LIST = {"res.partner": ModelAccess(read=("id", "name"))}
_PARTNER_READ_ALL_WRITE_NAME = {
    "res.partner": ModelAccess(read="all", write=("name",))
}


def _tool(mcp, name: str):
    tool = mcp._tool_manager.get_tool(name)
    assert tool is not None, f"tool '{name}' not registered"
    return tool


# --------------------------------------------------------------------------
# list_models
# --------------------------------------------------------------------------


def test_list_models_reads_allowlist_without_touching_backend() -> None:
    mcp = build_mcp_server("test", lambda: (_ for _ in ()).throw(AssertionError("backend touched")), allowed_models=_PARTNER_ALL)

    assert _tool(mcp, "list_models").fn() == ["res.partner"]


def test_list_models_empty_when_no_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend)

    assert _tool(mcp, "list_models").fn() == []


# --------------------------------------------------------------------------
# describe_model
# --------------------------------------------------------------------------


def test_describe_model_rejects_model_outside_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    with pytest.raises(ValueError, match="not in this server's allowed_models"):
        _tool(mcp, "describe_model").fn(model="res.users")


def test_describe_model_read_all_returns_every_field() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    result = _tool(mcp, "describe_model").fn(model="res.partner")

    assert set(result) == {"id", "name", "email"}
    assert result["name"] == {"string": "Name", "type": "char"}


def test_describe_model_read_list_filters_fields() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_READ_LIST)

    result = _tool(mcp, "describe_model").fn(model="res.partner")

    assert set(result) == {"id", "name"}


# --------------------------------------------------------------------------
# search_read
# --------------------------------------------------------------------------


def test_search_read_rejects_model_outside_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    with pytest.raises(ValueError, match="not in this server's allowed_models"):
        _tool(mcp, "search_read").fn(model="res.users", fields=["id"])


def test_search_read_all_requires_explicit_fields() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    with pytest.raises(ValueError, match="must be passed explicitly"):
        _tool(mcp, "search_read").fn(model="res.partner")


def test_search_read_all_with_fields_returns_projected_records() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    result = _tool(mcp, "search_read").fn(model="res.partner", fields=["id", "name"], limit=1)

    assert result == [{"id": 1, "name": "Azure Interior"}]


def test_search_read_field_list_defaults_to_that_list_when_fields_omitted() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_READ_LIST)

    result = _tool(mcp, "search_read").fn(model="res.partner", limit=1)

    assert result == [{"id": 1, "name": "Azure Interior"}]


def test_search_read_rejects_field_outside_read_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_READ_LIST)

    with pytest.raises(ValueError, match="not in the read allowlist"):
        _tool(mcp, "search_read").fn(model="res.partner", fields=["email"])


def test_search_read_rejects_domain_field_outside_read_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_READ_LIST)

    with pytest.raises(ValueError, match="domain field 'email' is not in the read allowlist"):
        _tool(mcp, "search_read").fn(
            model="res.partner", domain=[("email", "ilike", "x")], fields=["id"]
        )


def test_search_read_caps_limit_at_max() -> None:
    calls: dict[str, object] = {}

    class _Recording(FakeBackend):
        def search_read(self, model, domain=None, fields=None, limit=None, offset=0):
            calls["limit"] = limit
            return super().search_read(model, domain=domain, fields=fields, limit=limit, offset=offset)

    mcp = build_mcp_server("test", _Recording, allowed_models=_PARTNER_ALL)

    _tool(mcp, "search_read").fn(model="res.partner", fields=["id"], limit=1000)

    assert calls["limit"] == 100


# --------------------------------------------------------------------------
# search_count
# --------------------------------------------------------------------------


def test_search_count_rejects_model_outside_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    with pytest.raises(ValueError, match="not in this server's allowed_models"):
        _tool(mcp, "search_count").fn(model="res.users")


def test_search_count_returns_int() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    result = _tool(mcp, "search_count").fn(model="res.partner")

    assert result == 4


def test_search_count_rejects_domain_field_outside_read_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_READ_LIST)

    with pytest.raises(ValueError, match="domain field 'email' is not in the read allowlist"):
        _tool(mcp, "search_count").fn(model="res.partner", domain=[("email", "ilike", "x")])


# --------------------------------------------------------------------------
# read_group
# --------------------------------------------------------------------------


def test_read_group_rejects_model_outside_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    with pytest.raises(ValueError, match="not in this server's allowed_models"):
        _tool(mcp, "read_group").fn(model="res.users", fields=["name"], groupby=["name"])


def test_read_group_returns_one_row_per_group() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    result = _tool(mcp, "read_group").fn(
        model="res.partner",
        domain=[("name", "ilike", "Azure")],
        fields=["name"],
        groupby=["name"],
    )

    assert result == [{"name": "Azure Interior", "__count": 1}]


def test_read_group_rejects_aggregate_field_outside_read_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_READ_LIST)

    with pytest.raises(ValueError, match="not in the read allowlist"):
        _tool(mcp, "read_group").fn(model="res.partner", fields=["email"], groupby=["name"])


def test_read_group_rejects_groupby_field_outside_read_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_READ_LIST)

    with pytest.raises(ValueError, match="groupby field.*not in the read allowlist"):
        _tool(mcp, "read_group").fn(model="res.partner", fields=["name"], groupby=["email"])


def test_read_group_rejects_domain_field_outside_read_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_READ_LIST)

    with pytest.raises(ValueError, match="domain field 'email' is not in the read allowlist"):
        _tool(mcp, "read_group").fn(
            model="res.partner",
            domain=[("email", "ilike", "x")],
            fields=["name"],
            groupby=["name"],
        )


def test_read_group_caps_limit_at_max() -> None:
    calls: dict[str, object] = {}

    class _Recording(FakeBackend):
        def read_group(self, model, domain, fields, groupby, limit=None, offset=0):
            calls["limit"] = limit
            return super().read_group(
                model, domain=domain, fields=fields, groupby=groupby, limit=limit, offset=offset
            )

    mcp = build_mcp_server("test", _Recording, allowed_models=_PARTNER_ALL)

    _tool(mcp, "read_group").fn(model="res.partner", fields=["name"], groupby=["name"], limit=1000)

    assert calls["limit"] == 100


# --------------------------------------------------------------------------
# name_search
# --------------------------------------------------------------------------


def test_name_search_rejects_model_outside_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    with pytest.raises(ValueError, match="not in this server's allowed_models"):
        _tool(mcp, "name_search").fn(model="res.users")


def test_name_search_returns_id_name_pairs() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    result = _tool(mcp, "name_search").fn(model="res.partner", name="Azure")

    assert result == [(1, "Azure Interior")]


def test_name_search_rejects_domain_field_outside_read_allowlist() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_READ_LIST)

    with pytest.raises(ValueError, match="domain field 'email' is not in the read allowlist"):
        _tool(mcp, "name_search").fn(model="res.partner", domain=[("email", "ilike", "x")])


def test_name_search_caps_limit_at_max() -> None:
    calls: dict[str, object] = {}

    class _Recording(FakeBackend):
        def name_search(self, model, name="", domain=None, operator="ilike", limit=100):
            calls["limit"] = limit
            return super().name_search(model, name=name, domain=domain, operator=operator, limit=limit)

    mcp = build_mcp_server("test", _Recording, allowed_models=_PARTNER_ALL)

    _tool(mcp, "name_search").fn(model="res.partner", limit=1000)

    assert calls["limit"] == 100


# --------------------------------------------------------------------------
# create / write tool availability
# --------------------------------------------------------------------------


def test_create_and_write_not_registered_without_write_tools() -> None:
    mcp = build_mcp_server("test", FakeBackend, allowed_models=_PARTNER_ALL)

    assert mcp._tool_manager.get_tool("create") is None
    assert mcp._tool_manager.get_tool("write") is None


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


def test_create_without_confirm_does_not_write() -> None:
    backend = FakeBackend()
    mcp = build_mcp_server(
        "test", lambda: backend, write_tools={"create"}, allowed_models=_PARTNER_READ_ALL_WRITE_NAME
    )
    before = backend.search_read("res.partner")

    result = _tool(mcp, "create").fn(model="res.partner", values={"name": "New Co"})

    assert result == {"confirmed": False, "created": False, "preview": {"name": "New Co"}}
    assert backend.search_read("res.partner") == before


def test_create_with_confirm_writes_allowlisted_fields() -> None:
    backend = FakeBackend()
    mcp = build_mcp_server(
        "test", lambda: backend, write_tools={"create"}, allowed_models=_PARTNER_READ_ALL_WRITE_NAME
    )

    result = _tool(mcp, "create").fn(model="res.partner", values={"name": "New Co"}, confirm=True)

    assert result["confirmed"] is True
    assert result["created"] is True
    assert isinstance(result["id"], int)


def test_create_rejects_field_outside_write_allowlist() -> None:
    mcp = build_mcp_server(
        "test", FakeBackend, write_tools={"create"}, allowed_models=_PARTNER_READ_ALL_WRITE_NAME
    )

    with pytest.raises(ValueError, match="not in the write allowlist"):
        _tool(mcp, "create").fn(model="res.partner", values={"email": "x@example.com"})


def test_create_rejects_model_without_any_write_access() -> None:
    """`read: all` with no `write:` key means no write access at all,
    even though the `create` tool is registered on this server."""
    mcp = build_mcp_server("test", FakeBackend, write_tools={"create"}, allowed_models=_PARTNER_ALL)

    with pytest.raises(ValueError, match="not in the write allowlist"):
        _tool(mcp, "create").fn(model="res.partner", values={"name": "New Co"})


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------


def test_write_without_confirm_does_not_write() -> None:
    backend = FakeBackend()
    mcp = build_mcp_server(
        "test", lambda: backend, write_tools={"write"}, allowed_models=_PARTNER_READ_ALL_WRITE_NAME
    )
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
    mcp = build_mcp_server(
        "test", lambda: backend, write_tools={"write"}, allowed_models=_PARTNER_READ_ALL_WRITE_NAME
    )

    result = _tool(mcp, "write").fn(model="res.partner", ids=[1], values={"name": "Renamed"}, confirm=True)

    assert result["confirmed"] is True
    assert result["written"] is True
    updated = backend.read("res.partner", [1])
    assert updated[0]["name"] == "Renamed"


def test_write_rejects_empty_ids() -> None:
    mcp = build_mcp_server(
        "test", FakeBackend, write_tools={"write"}, allowed_models=_PARTNER_READ_ALL_WRITE_NAME
    )

    with pytest.raises(ValueError, match="must not be empty"):
        _tool(mcp, "write").fn(model="res.partner", ids=[], values={"name": "x"})


def test_write_rejects_more_than_max_ids() -> None:
    mcp = build_mcp_server(
        "test", FakeBackend, write_tools={"write"}, allowed_models=_PARTNER_READ_ALL_WRITE_NAME
    )

    with pytest.raises(ValueError, match="more than 100 ids"):
        _tool(mcp, "write").fn(model="res.partner", ids=list(range(101)), values={"name": "x"})


def test_write_rejects_field_outside_write_allowlist() -> None:
    mcp = build_mcp_server(
        "test", FakeBackend, write_tools={"write"}, allowed_models=_PARTNER_READ_ALL_WRITE_NAME
    )

    with pytest.raises(ValueError, match="not in the write allowlist"):
        _tool(mcp, "write").fn(model="res.partner", ids=[1], values={"email": "x@example.com"})
