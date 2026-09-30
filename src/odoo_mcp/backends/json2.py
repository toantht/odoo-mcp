"""Real Odoo 19+ backend over the External JSON-2 API.

Maps `Backend` methods to `POST {url}/json/2/<model>/<method>` calls:
bearer API key via `Authorization`, optional `X-Odoo-Database` header for
multi-db servers, all arguments passed as named JSON keys (JSON-2 has no
positional args). Network/HTTP/Odoo-side errors are caught here and
re-raised as `Json2Error` so tools only ever see one exception type,
never raw httpx errors.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv

from odoo_mcp.config.registry import RegistryError, get_server
from odoo_mcp.core.backend import Backend


class Json2Error(RuntimeError):
    """Wraps any Json2Backend failure: config, network, HTTP status, or
    an Odoo-side error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Json2Backend(Backend):
    """Calls Odoo's External JSON-2 API. One instance = one server/db."""

    def __init__(
        self,
        url: str,
        api_key: str,
        db: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        headers = {
            "Authorization": f"bearer {api_key}",
            "Content-Type": "application/json",
        }
        if db:
            headers["X-Odoo-Database"] = db
        self._client = httpx.Client(
            base_url=url.rstrip("/") + "/json/2/", headers=headers, timeout=timeout
        )

    @classmethod
    def from_env(cls, *, load_dotenv_file: bool = True) -> "Json2Backend":
        """Build from env vars; `ODOO_API_KEY` is always required.

        If `ODOO_MCP_SERVER` is set, `url`/`db` come from the Phase 5
        registry (`config/servers.yaml`, see `config/servers.example.yaml`
        and `odoo_mcp.config.registry`). Otherwise falls back to the
        Phase 2-4 `ODOO_URL` / `ODOO_DB` env vars directly.

        `load_dotenv_file=True` (default) loads a `.env` from the current
        directory first. Pass `False` if the caller already loaded env
        vars itself (e.g. `uv run --env-file .env`).
        """
        if load_dotenv_file:
            load_dotenv()

        api_key = os.environ.get("ODOO_API_KEY", "").strip()
        if not api_key:
            raise Json2Error("Missing ODOO_API_KEY env var (see .env.example).")

        server_id = os.environ.get("ODOO_MCP_SERVER", "").strip()
        if server_id:
            try:
                server = get_server(server_id)
            except RegistryError as exc:
                raise Json2Error(str(exc)) from exc
            if server.backend != "json2":
                raise Json2Error(
                    f"Server '{server_id}' is configured for backend "
                    f"'{server.backend}', not 'json2'."
                )
            return cls(server.url, api_key, db=server.db)

        url = os.environ.get("ODOO_URL", "").strip()
        db = os.environ.get("ODOO_DB", "").strip() or None
        if not url:
            raise Json2Error(
                "Missing ODOO_URL (or ODOO_MCP_SERVER) env var (see .env.example)."
            )
        return cls(url, api_key, db=db)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Json2Backend":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _post(self, model: str, method: str, body: dict[str, Any]) -> Any:
        try:
            response = self._client.post(f"{model}/{method}", json=body)
        except httpx.RequestError as exc:
            raise Json2Error(
                f"Network error calling {model}.{method}: {exc}"
            ) from exc

        if response.status_code >= 400:
            raise Json2Error(
                f"Odoo {model}.{method} failed: HTTP {response.status_code} "
                f"{response.text[:500]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise Json2Error(
                f"Non-JSON response from {model}.{method}: {response.text[:500]}"
            ) from exc

    def search_read(
        self,
        model: str,
        domain: list[Any] | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"domain": domain or [], "offset": offset}
        if fields:
            body["fields"] = fields
        if limit is not None:
            body["limit"] = limit
        return self._post(model, "search_read", body)

    def read(
        self,
        model: str,
        ids: list[int],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"ids": ids}
        if fields:
            body["fields"] = fields
        return self._post(model, "read", body)

    def search_count(self, model: str, domain: list[Any] | None = None) -> int:
        return self._post(model, "search_count", {"domain": domain or []})

    def read_group(
        self,
        model: str,
        domain: list[Any] | None,
        fields: list[str],
        groupby: list[str],
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "domain": domain or [],
            "fields": fields,
            "groupby": groupby,
            "offset": offset,
        }
        if limit is not None:
            body["limit"] = limit
        return self._post(model, "read_group", body)

    def name_search(
        self,
        model: str,
        name: str = "",
        domain: list[Any] | None = None,
        operator: str = "ilike",
        limit: int = 100,
    ) -> list[tuple[int, str]]:
        body: dict[str, Any] = {
            "name": name,
            "domain": domain or [],
            "operator": operator,
            "limit": limit,
        }
        return [tuple(pair) for pair in self._post(model, "name_search", body)]

    def create(self, model: str, values: dict[str, Any]) -> int:
        # JSON-2 create takes `vals_list` (a list of dicts) and returns a
        # list of new ids, even for a single record.
        result = self._post(model, "create", {"vals_list": [values]})
        return result[0] if isinstance(result, list) else result

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        return self._post(model, "write", {"ids": ids, "vals": values})

    def call(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Escape hatch for any other model method. JSON-2 has no
        positional arguments - pass record ids via `kwargs["ids"]`
        instead of `args`."""
        if args:
            raise Json2Error(
                "Json2Backend.call: positional args are not supported by "
                "JSON-2 - pass named arguments via kwargs instead "
                "(e.g. kwargs={'ids': [...]})."
            )
        return self._post(model, method, dict(kwargs or {}))

    def fields_get(
        self,
        model: str,
        attributes: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        body: dict[str, Any] = {}
        if attributes:
            body["attributes"] = attributes
        return self._post(model, "fields_get", body)
