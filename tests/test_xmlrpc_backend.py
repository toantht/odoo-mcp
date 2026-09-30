"""Phase 8 unit tests: `odoo_mcp.backends.xmlrpc.XmlRpcBackend`.

Mocks `xmlrpc.client.ServerProxy` (via `unittest.mock.patch`) - no
network, no real Odoo <=18 server, same spirit as `test_registry.py`.
Covers: login (success/failure), each `Backend` method mapping to
`execute_kw`, error wrapping (`Fault`/`ProtocolError`/`OSError` ->
`XmlRpcError`), and the two `from_env` code paths (registry vs raw env).
"""

from __future__ import annotations

import xmlrpc.client
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from odoo_mcp.backends.xmlrpc import XmlRpcBackend, XmlRpcError

_EXAMPLE_REGISTRY = Path(__file__).resolve().parent.parent / "config" / "servers.example.yaml"


def _proxies(*, uid: Any = 1, execute_kw_result: Any = None) -> tuple[MagicMock, MagicMock]:
    common = MagicMock()
    common.authenticate.return_value = uid
    obj = MagicMock()
    obj.execute_kw.return_value = execute_kw_result
    return common, obj


def _patched_backend(common: MagicMock, obj: MagicMock, **kwargs: Any) -> XmlRpcBackend:
    with patch(
        "odoo_mcp.backends.xmlrpc.xmlrpc.client.ServerProxy", side_effect=[common, obj]
    ):
        return XmlRpcBackend("https://odoo.test", "db", "admin", "s3cr3t", **kwargs)


def test_init_authenticates_and_stores_uid() -> None:
    common, obj = _proxies(uid=42)

    backend = _patched_backend(common, obj)

    common.authenticate.assert_called_once_with("db", "admin", "s3cr3t", {})
    assert backend._uid == 42


def test_init_raises_on_falsy_uid() -> None:
    common, obj = _proxies(uid=False)

    with pytest.raises(XmlRpcError, match="Authentication failed"):
        _patched_backend(common, obj)


def test_init_wraps_fault_during_login() -> None:
    common, obj = _proxies()
    common.authenticate.side_effect = xmlrpc.client.Fault(1, "bad credentials")

    with pytest.raises(XmlRpcError, match="Login failed.*bad credentials"):
        _patched_backend(common, obj)


def test_init_wraps_connection_error_during_login() -> None:
    common, obj = _proxies()
    common.authenticate.side_effect = ConnectionRefusedError("refused")

    with pytest.raises(XmlRpcError, match="Network error during XML-RPC login"):
        _patched_backend(common, obj)


def test_search_read_maps_to_execute_kw() -> None:
    common, obj = _proxies(execute_kw_result=[{"id": 1, "name": "Azure Interior"}])
    backend = _patched_backend(common, obj)

    result = backend.search_read(
        "res.partner",
        domain=[("name", "ilike", "Azure")],
        fields=["id", "name"],
        limit=5,
        offset=0,
    )

    assert result == [{"id": 1, "name": "Azure Interior"}]
    obj.execute_kw.assert_called_once_with(
        "db",
        backend._uid,
        "s3cr3t",
        "res.partner",
        "search_read",
        [[("name", "ilike", "Azure")]],
        {"fields": ["id", "name"], "limit": 5, "offset": 0},
    )


def test_read_maps_to_execute_kw() -> None:
    common, obj = _proxies(execute_kw_result=[{"id": 1, "name": "Azure Interior"}])
    backend = _patched_backend(common, obj)

    result = backend.read("res.partner", [1], fields=["id", "name"])

    assert result == [{"id": 1, "name": "Azure Interior"}]
    obj.execute_kw.assert_called_once_with(
        "db", backend._uid, "s3cr3t", "res.partner", "read", [[1]], {"fields": ["id", "name"]}
    )


def test_create_maps_to_execute_kw() -> None:
    common, obj = _proxies(execute_kw_result=7)
    backend = _patched_backend(common, obj)

    result = backend.create("res.partner", {"name": "New Co"})

    assert result == 7
    obj.execute_kw.assert_called_once_with(
        "db", backend._uid, "s3cr3t", "res.partner", "create", [{"name": "New Co"}], {}
    )


def test_write_maps_to_execute_kw() -> None:
    common, obj = _proxies(execute_kw_result=True)
    backend = _patched_backend(common, obj)

    result = backend.write("res.partner", [1, 2], {"active": False})

    assert result is True
    obj.execute_kw.assert_called_once_with(
        "db", backend._uid, "s3cr3t", "res.partner", "write", [[1, 2], {"active": False}], {}
    )


def test_call_passes_positional_and_keyword_args() -> None:
    common, obj = _proxies(execute_kw_result="ok")
    backend = _patched_backend(common, obj)

    result = backend.call("res.partner", "some_method", args=[1, 2], kwargs={"x": "y"})

    assert result == "ok"
    obj.execute_kw.assert_called_once_with(
        "db", backend._uid, "s3cr3t", "res.partner", "some_method", [1, 2], {"x": "y"}
    )


def test_execute_kw_wraps_fault() -> None:
    common, obj = _proxies()
    obj.execute_kw.side_effect = xmlrpc.client.Fault(2, "access denied")
    backend = _patched_backend(common, obj)

    with pytest.raises(XmlRpcError, match="access denied"):
        backend.read("res.partner", [1])


def test_execute_kw_wraps_protocol_error() -> None:
    common, obj = _proxies()
    obj.execute_kw.side_effect = xmlrpc.client.ProtocolError(
        "https://odoo.test/xmlrpc/2/object", 500, "Internal Server Error", {}
    )
    backend = _patched_backend(common, obj)

    with pytest.raises(XmlRpcError, match="HTTP error calling"):
        backend.read("res.partner", [1])


def test_execute_kw_wraps_os_error() -> None:
    common, obj = _proxies()
    obj.execute_kw.side_effect = TimeoutError("timed out")
    backend = _patched_backend(common, obj)

    with pytest.raises(XmlRpcError, match="Network error calling"):
        backend.read("res.partner", [1])


def test_from_env_uses_registry_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    common, obj = _proxies(uid=1)
    monkeypatch.setenv("ODOO_API_KEY", "s3cr3t")
    monkeypatch.setenv("ODOO_MCP_SERVER", "odoo_b")
    monkeypatch.setenv("ODOO_MCP_CONFIG", str(_EXAMPLE_REGISTRY))

    with patch(
        "odoo_mcp.backends.xmlrpc.xmlrpc.client.ServerProxy", side_effect=[common, obj]
    ):
        backend = XmlRpcBackend.from_env(load_dotenv_file=False)

    common.authenticate.assert_called_once_with(
        "legacy_prod", "mcp-integration@example.com", "s3cr3t", {}
    )
    assert isinstance(backend, XmlRpcBackend)


def test_from_env_registry_entry_wrong_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ODOO_API_KEY", "s3cr3t")
    monkeypatch.setenv("ODOO_MCP_SERVER", "odoo_a")
    monkeypatch.setenv("ODOO_MCP_CONFIG", str(_EXAMPLE_REGISTRY))

    with pytest.raises(XmlRpcError, match="not 'xmlrpc'"):
        XmlRpcBackend.from_env(load_dotenv_file=False)


def test_from_env_raw_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    common, obj = _proxies(uid=1)
    monkeypatch.delenv("ODOO_MCP_SERVER", raising=False)
    monkeypatch.setenv("ODOO_API_KEY", "s3cr3t")
    monkeypatch.setenv("ODOO_URL", "https://legacy.example.com")
    monkeypatch.setenv("ODOO_DB", "legacy_prod")
    monkeypatch.setenv("ODOO_LOGIN", "admin")

    with patch(
        "odoo_mcp.backends.xmlrpc.xmlrpc.client.ServerProxy", side_effect=[common, obj]
    ):
        backend = XmlRpcBackend.from_env(load_dotenv_file=False)

    common.authenticate.assert_called_once_with("legacy_prod", "admin", "s3cr3t", {})
    assert isinstance(backend, XmlRpcBackend)


def test_from_env_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_API_KEY", raising=False)

    with pytest.raises(XmlRpcError, match="ODOO_API_KEY"):
        XmlRpcBackend.from_env(load_dotenv_file=False)


def test_from_env_raw_fallback_missing_login_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ODOO_MCP_SERVER", raising=False)
    monkeypatch.setenv("ODOO_API_KEY", "s3cr3t")
    monkeypatch.setenv("ODOO_URL", "https://legacy.example.com")
    monkeypatch.setenv("ODOO_DB", "legacy_prod")
    monkeypatch.delenv("ODOO_LOGIN", raising=False)

    with pytest.raises(XmlRpcError, match="ODOO_URL/ODOO_DB/ODOO_LOGIN"):
        XmlRpcBackend.from_env(load_dotenv_file=False)
