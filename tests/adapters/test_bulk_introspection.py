"""Bulk introspection must agree with the per-object queries.

``SchemaIntrospector.build`` uses ``fetch_all_*`` when it can and falls back to
``fetch_columns``/``fetch_foreign_keys``/``fetch_indexes`` per object otherwise
(and the schema tree uses the per-object ones for lazy expansion). Two code
paths answering the same question is exactly where they drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from registro_adapters.introspect.sqlite import SQLiteIntrospector
from registro_adapters.sqlite_adapter import SQLiteAdapter
from registro_core.models.connection import Dsn

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_SCHEMA = [
    "CREATE TABLE parent (a INTEGER, b INTEGER, PRIMARY KEY (a, b))",
    "CREATE TABLE child (x INTEGER, y INTEGER, note TEXT NOT NULL DEFAULT 'n', "
    "FOREIGN KEY (x, y) REFERENCES parent (a, b))",
    "CREATE UNIQUE INDEX child_xy ON child (x, y)",
    "CREATE INDEX child_note ON child (note)",
    "CREATE VIEW child_view AS SELECT x FROM child",
]


@pytest.fixture
async def adapter() -> AsyncIterator[SQLiteAdapter]:
    a = SQLiteAdapter()
    await a.connect(Dsn.parse("sqlite:///:memory:"))
    for stmt in _SCHEMA:
        handle = await a.execute(stmt)
        async for _ in await a.stream(handle):
            pass
    try:
        yield a
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_bulk_columns_match_per_table(adapter: SQLiteAdapter) -> None:
    introspector = SQLiteIntrospector(adapter, adapter._conn)  # type: ignore[arg-type]
    bulk = await introspector.fetch_all_columns()

    for table in ("parent", "child", "child_view"):
        assert await introspector.fetch_columns("main", table) == bulk[("main", table)]


@pytest.mark.asyncio
async def test_bulk_foreign_keys_match_per_table(adapter: SQLiteAdapter) -> None:
    introspector = SQLiteIntrospector(adapter, adapter._conn)  # type: ignore[arg-type]
    bulk = await introspector.fetch_all_foreign_keys()

    assert await introspector.fetch_foreign_keys("main", "child") == bulk[("main", "child")]
    assert ("main", "parent") not in bulk
    # Composite FK columns must stay in declaration order.
    assert bulk[("main", "child")][0].columns == ["x", "y"]
    assert bulk[("main", "child")][0].referenced_columns == ["a", "b"]


@pytest.mark.asyncio
async def test_bulk_indexes_match_per_table(adapter: SQLiteAdapter) -> None:
    introspector = SQLiteIntrospector(adapter, adapter._conn)  # type: ignore[arg-type]
    bulk = await introspector.fetch_all_indexes()

    per_table = await introspector.fetch_indexes("main", "child")
    assert sorted(i.name for i in per_table) == sorted(i.name for i in bulk[("main", "child")])
    by_name = {i.name: i for i in bulk[("main", "child")]}
    assert by_name["child_xy"].unique is True
    assert by_name["child_xy"].columns == ["x", "y"]
    assert by_name["child_note"].unique is False


@pytest.mark.asyncio
async def test_eager_snapshot_matches_lazily_fetched_details(adapter: SQLiteAdapter) -> None:
    """The eager path (bulk) and the lazy path (per-object) must agree."""
    eager = await adapter.introspect()
    lazy = await adapter.introspect(lazy=True)

    eager_child = next(o for o in eager.schemas["main"] if o.name == "child")
    lazy_child = next(o for o in lazy.schemas["main"] if o.name == "child")
    assert lazy_child.columns == [], "lazy introspection should not fetch columns"

    assert eager_child.columns == await adapter.fetch_columns("main", "child")
    assert eager_child.foreign_keys == await adapter.fetch_foreign_keys("main", "child")
    assert sorted(i.name for i in eager_child.indexes) == sorted(
        i.name for i in await adapter.fetch_indexes("main", "child")
    )
