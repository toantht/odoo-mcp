"""Phase 8: Odoo <=18 backend over legacy XML-RPC.

Maps `Backend` methods to `execute_kw(db, uid, password, model, method,
args, kwargs)` on `{url}/xmlrpc/2/object`, after logging in via
`common.authenticate(db, login, password, {})` on `{url}/xmlrpc/2/common`
- unlike `Json2Backend`'s bearer key, XML-RPC has no "key = whole
identity" shortcut: every server needs an explicit `db` and a `login`
(username/email) alongside the password. The password here is still
`ODOO_API_KEY` - Odoo accepts an API key in place of the real password
for XML-RPC/JSON-RPC login since 14.0, so no separate secret is needed.

Network/XML-RPC/Odoo-side errors are all caught here and re-raised as
`XmlRpcError`, mirroring `Json2Error`, so tools only ever see one
exception type per backend, never raw `xmlrpc.client` errors.
"""

from __future__ import annotations

import os
import xmlrpc.client
from typing import Any
from urllib.parse import urlsplit

from dotenv import load_dotenv

from odoo_mcp.config.registry import RegistryError, get_server
from odoo_mcp.core.backend import Backend


class XmlRpcError(RuntimeError):
    """Wraps any XmlRpcBackend failure: config, network, HTTP/protocol
    error, login failure, or an Odoo-side fault."""


class _TimeoutTransport(xmlrpc.client.Transport):
    """Plain-HTTP `Transport` that applies a socket timeout - the stdlib
    `xmlrpc.client` has no `timeout=` kwarg on `ServerProxy` itself."""

    def __init__(self, timeout: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host: Any) -> Any:
        connection = super().make_connection(host)
        connection.timeout = self._timeout
        return connection


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    """Same as `_TimeoutTransport`, for `https://` servers."""

    def __init__(self, timeout: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._timeout = timeout

    def make_connection(self, host: Any) -> Any:
        connection = super().make_connection(host)
        connection.timeout = self._timeout
        return connection


def _build_transport(url: str, timeout: float) -> xmlrpc.client.Transport:
    transport_cls = _TimeoutSafeTransport if urlsplit(url).scheme == "https" else _TimeoutTransport
    return transport_cls(timeout)


class XmlRpcBackend(Backend):
    """Calls Odoo's legacy XML-RPC API. One instance = one server/db/user."""

    def __init__(
        self,
        url: str,
        db: str,
        login: str,
        password: str,
        *,
        timeout: float = 30.0,
    ) -> None:
        base_url = url.rstrip("/")
        self._db = db
        self._password = password
        self._common = xmlrpc.client.ServerProxy(
            f"{base_url}/xmlrpc/2/common",
            transport=_build_transport(base_url, timeout),
            allow_none=True,
        )
        self._object = xmlrpc.client.ServerProxy(
            f"{base_url}/xmlrpc/2/object",
            transport=_build_transport(base_url, timeout),
            allow_none=True,
        )

        try:
            uid = self._common.authenticate(db, login, password, {})
        except xmlrpc.client.Fault as exc:
            raise XmlRpcError(
                f"Login failed for db={db!r} login={login!r}: {exc.faultString}"
            ) from exc
        except xmlrpc.client.ProtocolError as exc:
            raise XmlRpcError(
                f"HTTP error during XML-RPC login: {exc.errcode} {exc.errmsg}"
            ) from exc
        except OSError as exc:
            raise XmlRpcError(f"Network error during XML-RPC login: {exc}") from exc

        if not uid:
            raise XmlRpcError(
                f"Authentication failed for db={db!r} login={login!r} "
                "(check the login/API key and database name)."
            )
        self._uid = uid

    @classmethod
    def from_env(cls, *, load_dotenv_file: bool = True) -> "XmlRpcBackend":
        """Build from env vars; `ODOO_API_KEY` is always required (used
        as the XML-RPC password).

        If `ODOO_MCP_SERVER` is set, `url`/`db`/`login` come from the
        Phase 5 registry (`config/servers.yaml`) - that server's `db`
        and `login` fields are required (see the Phase 8 note in
        `config/registry.py`). Otherwise falls back to `ODOO_URL` /
        `ODOO_DB` / `ODOO_LOGIN` env vars directly.

        `load_dotenv_file=True` (default) loads a `.env` from the
        current directory first. Pass `False` if the caller already
        loaded env vars itself.
        """
        if load_dotenv_file:
            load_dotenv()

        api_key = os.environ.get("ODOO_API_KEY", "").strip()
        if not api_key:
            raise XmlRpcError("Missing ODOO_API_KEY env var (see .env.example).")

        server_id = os.environ.get("ODOO_MCP_SERVER", "").strip()
        if server_id:
            try:
                server = get_server(server_id)
            except RegistryError as exc:
                raise XmlRpcError(str(exc)) from exc
            if server.backend != "xmlrpc":
                raise XmlRpcError(
                    f"Server '{server_id}' is configured for backend "
                    f"'{server.backend}', not 'xmlrpc'."
                )
            if not server.db:
                raise XmlRpcError(
                    f"Server '{server_id}' is missing 'db' in servers.yaml "
                    "(required for the xmlrpc backend)."
                )
            if not server.login:
                raise XmlRpcError(
                    f"Server '{server_id}' is missing 'login' in servers.yaml "
                    "(required for the xmlrpc backend)."
                )
            return cls(server.url, server.db, server.login, api_key)

        url = os.environ.get("ODOO_URL", "").strip()
        db = os.environ.get("ODOO_DB", "").strip()
        login = os.environ.get("ODOO_LOGIN", "").strip()
        if not url or not db or not login:
            raise XmlRpcError(
                "Missing ODOO_URL/ODOO_DB/ODOO_LOGIN (or ODOO_MCP_SERVER) "
                "env vars (see .env.example)."
            )
        return cls(url, db, login, api_key)

    def close(self) -> None:
        for proxy in (self._common, self._object):
            transport = getattr(proxy, "_ServerProxy__transport", None)
            connection = getattr(transport, "_connection", (None, None))[1]
            if connection is not None:
                connection.close()

    def __enter__(self) -> "XmlRpcBackend":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _execute_kw(
        self, model: str, method: str, args: list[Any], kwargs: dict[str, Any]
    ) -> Any:
        try:
            return self._object.execute_kw(
                self._db, self._uid, self._password, model, method, args, kwargs
            )
        except xmlrpc.client.Fault as exc:
            raise XmlRpcError(
                f"Odoo {model}.{method} failed: {exc.faultString}"
            ) from exc
        except xmlrpc.client.ProtocolError as exc:
            raise XmlRpcError(
                f"HTTP error calling {model}.{method}: {exc.errcode} {exc.errmsg}"
            ) from exc
        except OSError as exc:
            raise XmlRpcError(
                f"Network error calling {model}.{method}: {exc}"
            ) from exc

    def search_read(
        self,
        model: str,
        domain: list[Any] | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"offset": offset}
        if fields:
            kwargs["fields"] = fields
        if limit is not None:
            kwargs["limit"] = limit
        return self._execute_kw(model, "search_read", [domain or []], kwargs)

    def read(
        self,
        model: str,
        ids: list[int],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        kwargs = {"fields": fields} if fields else {}
        return self._execute_kw(model, "read", [ids], kwargs)

    def search_count(self, model: str, domain: list[Any] | None = None) -> int:
        return self._execute_kw(model, "search_count", [domain or []], {})

    def read_group(
        self,
        model: str,
        domain: list[Any] | None,
        fields: list[str],
        groupby: list[str],
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"offset": offset}
        if limit is not None:
            kwargs["limit"] = limit
        return self._execute_kw(
            model, "read_group", [domain or [], fields, groupby], kwargs
        )

    def name_search(
        self,
        model: str,
        name: str = "",
        domain: list[Any] | None = None,
        operator: str = "ilike",
        limit: int = 100,
    ) -> list[tuple[int, str]]:
        # Positional, not keyword: the domain parameter is named `args`
        # on Odoo <=18 (this backend's target) and `domain` from 19 on -
        # positional args side-step the rename either way.
        args = [name, domain or [], operator, limit]
        return [tuple(pair) for pair in self._execute_kw(model, "name_search", args, {})]

    def create(self, model: str, values: dict[str, Any]) -> int:
        return self._execute_kw(model, "create", [values], {})

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        return self._execute_kw(model, "write", [ids, values], {})

    def call(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Escape hatch for any other model method - unlike JSON-2,
        XML-RPC supports real positional `args`."""
        return self._execute_kw(model, method, list(args or []), dict(kwargs or {}))

    def fields_get(
        self,
        model: str,
        attributes: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        kwargs = {"attributes": attributes} if attributes else {}
        return self._execute_kw(model, "fields_get", [], kwargs)
