"""Abstract backend interface for Odoo MCP tools.

Tools call these methods only - never the underlying transport (JSON-2,
XML-RPC, ORM). Swapping the backend must never require tool/schema
changes. Phase 1 ships `backends/fake.py`; real implementations
(`backends/json2.py`, `backends/xmlrpc.py`) land in later phases.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Backend(ABC):
    """Common read/write contract every Odoo backend must implement."""

    @abstractmethod
    def search_read(
        self,
        model: str,
        domain: list[Any] | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Search + read in one call, mirroring Odoo's `search_read`."""
        raise NotImplementedError

    @abstractmethod
    def read(
        self,
        model: str,
        ids: list[int],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Read specific records by id."""
        raise NotImplementedError

    @abstractmethod
    def search_count(self, model: str, domain: list[Any] | None = None) -> int:
        """Count records matching `domain`, mirroring Odoo's
        `search_count` - one integer, never rows. Use this instead of
        paging `search_read` just to total how many records match."""
        raise NotImplementedError

    @abstractmethod
    def read_group(
        self,
        model: str,
        domain: list[Any] | None,
        fields: list[str],
        groupby: list[str],
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Group + aggregate in one call, mirroring Odoo's `read_group`.
        `fields` follows Odoo's own syntax (`"amount_total:sum"`, plain
        `"field"` for the default aggregation). Returns one dict per
        group, never per record."""
        raise NotImplementedError

    @abstractmethod
    def name_search(
        self,
        model: str,
        name: str = "",
        domain: list[Any] | None = None,
        operator: str = "ilike",
        limit: int = 100,
    ) -> list[tuple[int, str]]:
        """Resolve a display-name substring to `(id, display_name)`
        pairs, mirroring Odoo's `name_search` - lets a caller find a
        record's id without a full `search_read`."""
        raise NotImplementedError

    @abstractmethod
    def create(self, model: str, values: dict[str, Any]) -> int:
        """Create one record, return its id."""
        raise NotImplementedError

    @abstractmethod
    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        """Update records by id."""
        raise NotImplementedError

    @abstractmethod
    def call(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        """Escape hatch for any other model method not covered above."""
        raise NotImplementedError

    @abstractmethod
    def fields_get(
        self,
        model: str,
        attributes: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Return `{field_name: {attribute: value, ...}, ...}` for a
        model, mirroring Odoo's `fields_get`. `attributes=None` returns
        every attribute Odoo knows about; passing a list narrows each
        field's dict to just those keys (e.g. `["string", "type"]`)."""
        raise NotImplementedError
