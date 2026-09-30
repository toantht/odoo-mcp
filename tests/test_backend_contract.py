"""Phase 8 contract test: `FakeBackend`, `Json2Backend`, and
`XmlRpcBackend` must expose the exact same `Backend.search_read`
contract - same call, same shape of result - regardless of transport
(in-memory / JSON-2 HTTP / XML-RPC).

Network and XML-RPC calls are mocked here; this proves the *interface*
is uniform (the plan's "lõi dùng chung, vỏ mỏng"), not that any one
backend's real wire protocol works - see each backend's own smoke
test/unit tests for that (`scripts/smoke_tool.py`,
`tests/test_xmlrpc_backend.py`).
"""

from __future__ import annotations

from typing import Any, Callable
from unittest.mock import MagicMock, patch

import httpx
import pytest

from odoo_mcp.backends.fake import FakeBackend
from odoo_mcp.backends.json2 import Json2Backend
from odoo_mcp.backends.xmlrpc import XmlRpcBackend
from odoo_mcp.core.backend import Backend

_PARTNER = {"id": 1, "name": "Azure Interior", "email": "azure.interior24@example.com"}

# Matches `FakeBackend`'s static `res.partner` schema exactly, so the
# fields_get contract test can assert one equality across all three
# backends (fake computes this for real; json2/xmlrpc are mocked to
# return it) - same trick as `_PARTNER` above for `search_read`.
_FIELDS = {
    "id": {"string": "ID", "type": "integer"},
    "name": {"string": "Name", "type": "char"},
    "email": {"string": "Email", "type": "char"},
}

# search_count/read_group/name_search fixtures - mocked json2/xmlrpc
# return these regardless of args, matching _PARTNER/_FIELDS above.
_COUNT = 4
_GROUPS = [{"name": "Azure Interior", "__count": 1}]
_NAME_SEARCH_RESULT = [[1, "Azure Interior"]]  # JSON has no tuples on the wire


def _fake_backend() -> Backend:
    return FakeBackend()


def _json2_post_side_effect(path: str, **_: Any) -> httpx.Response:
    """Route the mocked `_client.post` by endpoint: `search_read` (used
    by the existing tests) returns `[_PARTNER]`, `fields_get` returns
    `_FIELDS`, and so on for the newer count/group/name-search methods."""
    if path.endswith("/fields_get"):
        return httpx.Response(200, json=_FIELDS)
    if path.endswith("/search_count"):
        return httpx.Response(200, json=_COUNT)
    if path.endswith("/read_group"):
        return httpx.Response(200, json=_GROUPS)
    if path.endswith("/name_search"):
        return httpx.Response(200, json=_NAME_SEARCH_RESULT)
    return httpx.Response(200, json=[_PARTNER])


def _json2_backend() -> Backend:
    backend = Json2Backend("https://odoo.test", "fake-key")
    backend._client.post = MagicMock(  # type: ignore[method-assign]
        side_effect=_json2_post_side_effect
    )
    return backend


def _xmlrpc_execute_kw_side_effect(
    _db: str, _uid: int, _password: str, _model: str, method: str, *_: Any
) -> Any:
    """Route the mocked `execute_kw` by method name, mirroring
    `_json2_post_side_effect` above."""
    if method == "fields_get":
        return _FIELDS
    if method == "search_count":
        return _COUNT
    if method == "read_group":
        return _GROUPS
    if method == "name_search":
        return _NAME_SEARCH_RESULT
    return [_PARTNER]


def _xmlrpc_backend() -> Backend:
    common = MagicMock()
    common.authenticate.return_value = 1
    obj = MagicMock()
    obj.execute_kw.side_effect = _xmlrpc_execute_kw_side_effect
    with patch(
        "odoo_mcp.backends.xmlrpc.xmlrpc.client.ServerProxy", side_effect=[common, obj]
    ):
        return XmlRpcBackend("https://odoo.test", "db", "admin", "fake-key")


@pytest.mark.parametrize(
    "make_backend",
    [_fake_backend, _json2_backend, _xmlrpc_backend],
    ids=["fake", "json2", "xmlrpc"],
)
def test_search_read_same_contract_across_backends(
    make_backend: Callable[[], Backend],
) -> None:
    backend = make_backend()

    result = backend.search_read(
        "res.partner",
        domain=[("name", "ilike", "Azure")],
        fields=["id", "name", "email"],
        limit=5,
    )

    assert result == [_PARTNER]


@pytest.mark.parametrize(
    "make_backend",
    [_fake_backend, _json2_backend, _xmlrpc_backend],
    ids=["fake", "json2", "xmlrpc"],
)
def test_search_read_default_args_same_contract(
    make_backend: Callable[[], Backend],
) -> None:
    """`domain`/`fields`/`limit` are all optional - every backend must
    accept the bare call `search_read(model)`, matching `Backend`'s ABC
    signature exactly (this is what actually breaks if a backend drifts
    from the shared interface)."""
    backend = make_backend()

    backend.search_read("res.partner")  # must not raise (TypeError, etc.)


@pytest.mark.parametrize(
    "make_backend",
    [_fake_backend, _json2_backend, _xmlrpc_backend],
    ids=["fake", "json2", "xmlrpc"],
)
def test_search_count_same_contract_across_backends(
    make_backend: Callable[[], Backend],
) -> None:
    """Empty domain so `FakeBackend`'s real count (all 4 seeded
    partners) lines up with json2/xmlrpc's mocked `_COUNT` - same trick
    `_PARTNER`/`_FIELDS` use above."""
    backend = make_backend()

    result = backend.search_count("res.partner", domain=[])

    assert result == _COUNT


@pytest.mark.parametrize(
    "make_backend",
    [_fake_backend, _json2_backend, _xmlrpc_backend],
    ids=["fake", "json2", "xmlrpc"],
)
def test_search_count_default_args_same_contract(
    make_backend: Callable[[], Backend],
) -> None:
    """`domain` is optional - every backend must accept the bare call
    `search_count(model)`."""
    backend = make_backend()

    backend.search_count("res.partner")  # must not raise (TypeError, etc.)


@pytest.mark.parametrize(
    "make_backend",
    [_fake_backend, _json2_backend, _xmlrpc_backend],
    ids=["fake", "json2", "xmlrpc"],
)
def test_read_group_same_contract_across_backends(
    make_backend: Callable[[], Backend],
) -> None:
    """Domain narrows `FakeBackend`'s real grouping down to the single
    "Azure Interior" group, matching json2/xmlrpc's mocked `_GROUPS`."""
    backend = make_backend()

    result = backend.read_group(
        "res.partner",
        domain=[("name", "ilike", "Azure")],
        fields=["name"],
        groupby=["name"],
        limit=5,
    )

    assert result == _GROUPS


@pytest.mark.parametrize(
    "make_backend",
    [_fake_backend, _json2_backend, _xmlrpc_backend],
    ids=["fake", "json2", "xmlrpc"],
)
def test_name_search_same_contract_across_backends(
    make_backend: Callable[[], Backend],
) -> None:
    backend = make_backend()

    result = backend.name_search("res.partner", name="Azure")

    assert result == [(1, "Azure Interior")]


@pytest.mark.parametrize(
    "make_backend",
    [_fake_backend, _json2_backend, _xmlrpc_backend],
    ids=["fake", "json2", "xmlrpc"],
)
def test_name_search_default_args_same_contract(
    make_backend: Callable[[], Backend],
) -> None:
    """Every arg but `model` is optional - every backend must accept the
    bare call `name_search(model)`."""
    backend = make_backend()

    backend.name_search("res.partner")  # must not raise (TypeError, etc.)


@pytest.mark.parametrize(
    "make_backend",
    [_fake_backend, _json2_backend, _xmlrpc_backend],
    ids=["fake", "json2", "xmlrpc"],
)
def test_fields_get_same_contract_across_backends(
    make_backend: Callable[[], Backend],
) -> None:
    backend = make_backend()

    result = backend.fields_get("res.partner", attributes=["string", "type"])

    assert result == _FIELDS


@pytest.mark.parametrize(
    "make_backend",
    [_fake_backend, _json2_backend, _xmlrpc_backend],
    ids=["fake", "json2", "xmlrpc"],
)
def test_fields_get_default_args_same_contract(
    make_backend: Callable[[], Backend],
) -> None:
    """`attributes` is optional - every backend must accept the bare
    call `fields_get(model)`, matching `Backend`'s ABC signature exactly."""
    backend = make_backend()

    backend.fields_get("res.partner")  # must not raise (TypeError, etc.)
