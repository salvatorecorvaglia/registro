"""Tests for the shared SchemaIntrospector template-method base."""

from __future__ import annotations

import pytest

from registro_adapters._introspect import SchemaIntrospector
from registro_core.models.schema import ForeignKey, Index, Table, TableColumn, View


def _col(name: str) -> TableColumn:
    return TableColumn(name=name, type_native="text")


class _StubIntrospector(SchemaIntrospector):
    """In-memory introspector driven by canned catalog data."""

    def __init__(self, objects: list[tuple[str, str, str]]) -> None:
        self._objects = objects
        self.fk_calls: list[tuple[str, str]] = []
        self.index_calls: list[tuple[str, str]] = []

    def connection_id(self) -> str:
        return "conn-1"

    def database_name(self) -> str | None:
        return "testdb"

    async def list_objects(self) -> list[tuple[str, str, str]]:
        return self._objects

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        return [_col(f"{name}_id")]

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        self.fk_calls.append((schema, name))
        return [
            ForeignKey(name="fk", columns=["a"], referenced_table="other", referenced_columns=["b"])
        ]

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        self.index_calls.append((schema, name))
        return [Index(name="ix", columns=["a"])]


@pytest.mark.asyncio
async def test_build_dispatches_table_vs_view() -> None:
    introspector = _StubIntrospector(
        [
            ("public", "users", "table"),
            ("public", "active_users", "view"),
        ]
    )
    snap = await introspector.build()

    assert snap.connection_id == "conn-1"
    assert snap.database == "testdb"
    objects = {o.name: o for o in snap.schemas["public"]}
    assert isinstance(objects["users"], Table)
    assert isinstance(objects["active_users"], View)


@pytest.mark.asyncio
async def test_build_fetches_fks_and_indexes_only_for_tables() -> None:
    introspector = _StubIntrospector(
        [
            ("public", "users", "table"),
            ("public", "active_users", "view"),
        ]
    )
    snap = await introspector.build()

    users = next(o for o in snap.schemas["public"] if o.name == "users")
    assert len(users.foreign_keys) == 1
    assert len(users.indexes) == 1
    # Views must not trigger FK/index catalog queries.
    assert introspector.fk_calls == [("public", "users")]
    assert introspector.index_calls == [("public", "users")]


@pytest.mark.asyncio
async def test_build_groups_objects_by_schema() -> None:
    introspector = _StubIntrospector(
        [
            ("public", "a", "table"),
            ("sales", "b", "table"),
        ]
    )
    snap = await introspector.build()
    assert set(snap.schemas) == {"public", "sales"}
    assert snap.object_count == 2


@pytest.mark.asyncio
async def test_fk_and_index_defaults_are_empty() -> None:
    """A minimal subclass that overrides only the required hooks."""

    class _Minimal(SchemaIntrospector):
        def connection_id(self) -> str:
            return "c"

        def database_name(self) -> str | None:
            return None

        async def list_objects(self) -> list[tuple[str, str, str]]:
            return [("public", "t", "table")]

        async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
            return [_col("x")]

    snap = await _Minimal().build()
    table = snap.schemas["public"][0]
    assert table.foreign_keys == []
    assert table.indexes == []


@pytest.mark.asyncio
async def test_bulk_introspect_hooks() -> None:
    """Introspector using bulk hooks bypasses per-object fetch methods."""

    class _BulkIntrospector(_StubIntrospector):
        async def fetch_all_columns(self) -> dict[tuple[str, str], list[TableColumn]]:
            return {("public", "users"): [_col("id")], ("public", "active_users"): [_col("id")]}

        async def fetch_all_foreign_keys(self) -> dict[tuple[str, str], list[ForeignKey]]:
            return {
                ("public", "users"): [
                    ForeignKey(
                        name="fk1",
                        columns=["role_id"],
                        referenced_table="roles",
                        referenced_columns=["id"],
                    )
                ]
            }

        async def fetch_all_indexes(self) -> dict[tuple[str, str], list[Index]]:
            return {("public", "users"): [Index(name="idx_users_id", columns=["id"])]}

    introspector = _BulkIntrospector(
        [
            ("public", "users", "table"),
            ("public", "active_users", "view"),
        ]
    )
    snap = await introspector.build()
    users = next(o for o in snap.schemas["public"] if o.name == "users")

    assert len(users.columns) == 1
    assert users.columns[0].name == "id"
    assert len(users.foreign_keys) == 1
    assert users.foreign_keys[0].name == "fk1"
    assert len(users.indexes) == 1
    assert users.indexes[0].name == "idx_users_id"
    # Per-object methods were bypassed due to bulk hooks
    assert introspector.fk_calls == []
    assert introspector.index_calls == []
