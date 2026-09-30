"""In-memory fake backend for Phase 1 - no network, no real Odoo.

Lets `core.tools` and the CLI smoke test be exercised end-to-end before
Phase 2 wires up the real `Json2Backend`.
"""

from __future__ import annotations

from typing import Any

from odoo_mcp.core.backend import Backend

_FAKE_PARTNERS: list[dict[str, Any]] = [
    {"id": 1, "name": "Azure Interior", "email": "azure.interior24@example.com"},
    {"id": 2, "name": "Deco Addict", "email": "deco.addict82@example.com"},
    {"id": 3, "name": "Lumber Inc", "email": "lumber.inc55@example.com"},
    {"id": 4, "name": "Wood Corner", "email": "wood.corner26@example.com"},
]

# Phase 11: static `fields_get` schema, matching the `_FAKE_PARTNERS`
# shape above (id/name/email only - real backends return every field
# Odoo knows about).
_FAKE_FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    "res.partner": {
        "id": {"string": "ID", "type": "integer"},
        "name": {"string": "Name", "type": "char"},
        "email": {"string": "Email", "type": "char"},
    },
}


class FakeBackend(Backend):
    """Hard-coded in-memory data. Only `res.partner` is seeded."""

    def __init__(self) -> None:
        self._data: dict[str, list[dict[str, Any]]] = {
            "res.partner": [dict(p) for p in _FAKE_PARTNERS],
        }

    def search_read(
        self,
        model: str,
        domain: list[Any] | None = None,
        fields: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        records = _apply_domain(self._data.get(model, []), domain or [])
        records = records[offset:]
        if limit is not None:
            records = records[:limit]
        return _project(records, fields)

    def read(
        self,
        model: str,
        ids: list[int],
        fields: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        id_set = set(ids)
        records = [r for r in self._data.get(model, []) if r["id"] in id_set]
        return _project(records, fields)

    def search_count(self, model: str, domain: list[Any] | None = None) -> int:
        return len(_apply_domain(self._data.get(model, []), domain or []))

    def read_group(
        self,
        model: str,
        domain: list[Any] | None,
        fields: list[str],
        groupby: list[str],
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        # Phase-appropriate minimum: group by the first `groupby` field
        # only (matches `lazy=True`, Odoo's own default), "field:count"
        # aggregation only - just enough for the generic tool's tests,
        # not a full read_group reimplementation.
        records = _apply_domain(self._data.get(model, []), domain or [])
        key_field = groupby[0] if groupby else None
        groups: dict[Any, list[dict[str, Any]]] = {}
        for record in records:
            key = record.get(key_field) if key_field else None
            groups.setdefault(key, []).append(record)

        result: list[dict[str, Any]] = []
        for key, group_records in groups.items():
            row: dict[str, Any] = {key_field: key} if key_field else {}
            row["__count"] = len(group_records)
            for field_spec in fields:
                field_name = field_spec.split(":")[0]
                if field_name == key_field:
                    continue
                values = [r.get(field_name) for r in group_records if r.get(field_name) is not None]
                row[field_name] = sum(values) if values else 0
            result.append(row)
        result = result[offset:]
        if limit is not None:
            result = result[:limit]
        return result

    def name_search(
        self,
        model: str,
        name: str = "",
        domain: list[Any] | None = None,
        operator: str = "ilike",
        limit: int = 100,
    ) -> list[tuple[int, str]]:
        records = _apply_domain(self._data.get(model, []), domain or [])
        needle = name.lower()
        matches = [r for r in records if needle in str(r.get("name", "")).lower()]
        return [(r["id"], str(r.get("name", ""))) for r in matches[:limit]]

    def create(self, model: str, values: dict[str, Any]) -> int:
        records = self._data.setdefault(model, [])
        new_id = max((r["id"] for r in records), default=0) + 1
        records.append({"id": new_id, **values})
        return new_id

    def write(self, model: str, ids: list[int], values: dict[str, Any]) -> bool:
        id_set = set(ids)
        for record in self._data.get(model, []):
            if record["id"] in id_set:
                record.update(values)
        return True

    def call(
        self,
        model: str,
        method: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> Any:
        raise NotImplementedError(
            f"FakeBackend.call: {model}.{method} not supported in Phase 1"
        )

    def fields_get(
        self,
        model: str,
        attributes: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        fields = _FAKE_FIELDS.get(model)
        if fields is None:
            raise NotImplementedError(
                f"FakeBackend.fields_get: {model} has no static schema"
            )
        if not attributes:
            return {name: dict(attrs) for name, attrs in fields.items()}
        return {
            name: {k: v for k, v in attrs.items() if k in attributes}
            for name, attrs in fields.items()
        }


def _project(
    records: list[dict[str, Any]], fields: list[str] | None
) -> list[dict[str, Any]]:
    if not fields:
        return [dict(r) for r in records]
    return [{f: r.get(f) for f in fields} for r in records]


def _apply_domain(
    records: list[dict[str, Any]], domain: list[Any]
) -> list[dict[str, Any]]:
    """Support just enough of Odoo domain syntax for Phase 1: a flat list
    of ("field", operator, value) tuples, ANDed together. No "|"/"&"
    prefix logic - real domain parsing arrives with the real backends.
    """
    result = records
    for clause in domain:
        if not isinstance(clause, (list, tuple)) or len(clause) != 3:
            continue
        field, operator, value = clause
        if operator == "ilike":
            needle = str(value).lower()
            result = [r for r in result if needle in str(r.get(field, "")).lower()]
        elif operator == "=":
            result = [r for r in result if r.get(field) == value]
        else:
            raise NotImplementedError(
                f"FakeBackend domain operator not supported: {operator!r}"
            )
    return result
