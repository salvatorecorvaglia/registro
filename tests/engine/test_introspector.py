"""Tests for the Introspector caching wrapper."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from registro_adapters.sqlite_adapter import SQLiteAdapter
from registro_core.events import EventBus, SchemaRefreshed
from registro_core.models.connection import Dsn
from registro_core.models.schema import ForeignKey, Index, SchemaSnapshot, TableColumn
from registro_engine.introspector import Introspector

if TYPE_CHECKING:
    from registro_core.protocols.adapter import DatabaseAdapter


class _FakeAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.last_lazy: bool | None = None

    async def introspect(self, *, lazy: bool = False) -> SchemaSnapshot:
        self.calls += 1
        self.last_lazy = lazy
        return SchemaSnapshot(connection_id=f"conn-{self.calls}")

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        return []

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        return []

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        return []


async def _exec(adapter: SQLiteAdapter, sql: str) -> None:
    """Run *sql* to completion against a real adapter."""
    handle = await adapter.execute(sql)
    async for _ in await adapter.stream(handle):
        pass


@pytest.mark.asyncio
async def test_snapshot_is_cached_until_invalidated() -> None:
    adapter = _FakeAdapter()
    intro = Introspector(cast("DatabaseAdapter", adapter), EventBus())

    first = await intro.snapshot()
    second = await intro.snapshot()
    assert first is second
    assert adapter.calls == 1

    intro.invalidate()
    third = await intro.snapshot()
    assert third is not first
    assert adapter.calls == 2


@pytest.mark.asyncio
async def test_refresh_forces_a_new_snapshot() -> None:
    adapter = _FakeAdapter()
    intro = Introspector(cast("DatabaseAdapter", adapter), EventBus())

    first = await intro.snapshot()
    refreshed = await intro.refresh()
    assert refreshed is not first
    assert adapter.calls == 2


@pytest.mark.asyncio
async def test_schema_refreshed_published_once_per_introspect() -> None:
    bus = EventBus()
    received: list[SchemaRefreshed] = []
    # Deterministic wait: the handler signals once the expected events have
    # arrived, rather than the test spinning the loop a guessed number of times.
    expected = asyncio.Event()

    async def on_refresh(ev: SchemaRefreshed) -> None:
        received.append(ev)
        if len(received) >= 2:
            expected.set()

    bus.subscribe(SchemaRefreshed, on_refresh)
    adapter = _FakeAdapter()
    intro = Introspector(cast("DatabaseAdapter", adapter), bus)

    await intro.snapshot()  # introspects -> publishes
    await intro.snapshot()  # cache hit -> no publish
    await intro.refresh()  # introspects -> publishes

    await asyncio.wait_for(expected.wait(), timeout=2)
    # Settle the loop so a third, unwanted publish would have landed by now.
    await asyncio.sleep(0)
    assert len(received) == 2
    await bus.close()


@pytest.mark.asyncio
async def test_snapshot_and_refresh_support_lazy() -> None:
    adapter = _FakeAdapter()
    intro = Introspector(cast("DatabaseAdapter", adapter), EventBus())

    await intro.snapshot(lazy=True)
    assert adapter.last_lazy is True

    await intro.refresh(lazy=False)
    assert adapter.last_lazy is False


@pytest.mark.asyncio
async def test_lazy_cache_does_not_satisfy_a_full_snapshot() -> None:
    """A lazy snapshot holds no columns, so it must not answer a full request.

    The cache used to key on presence alone: once a lazy snapshot was taken
    (which is what the TUI's schema refresh does), every later
    ``snapshot(lazy=False)`` — including one from a plugin handed the
    introspector as a service — silently got back an empty schema.
    """
    adapter = SQLiteAdapter()
    await adapter.connect(Dsn.parse("sqlite:///:memory:"))
    bus = EventBus()
    try:
        handle = await adapter.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        async for _ in await adapter.stream(handle):
            pass

        introspector = Introspector(adapter, bus)

        lazy = await introspector.snapshot(lazy=True)
        assert lazy.schemas["main"][0].columns == [], "precondition: lazy skips columns"

        full = await introspector.snapshot(lazy=False)
        assert [c.name for c in full.schemas["main"][0].columns] == ["id", "name"]

        # ...and the full snapshot is now cached, so a lazy request reuses it
        # rather than throwing away work.
        again = await introspector.snapshot(lazy=True)
        assert again is full
    finally:
        await bus.close()
        await adapter.close()


# ---- update_object ----
#
# The schema tree calls this after lazily loading an object's columns, indexes
# and foreign keys, so the cache reflects what the user just expanded. None of
# it was covered.


async def test_update_object_fills_in_lazily_loaded_metadata() -> None:
    adapter = SQLiteAdapter()
    await adapter.connect(Dsn.parse("sqlite:///:memory:"))
    bus = EventBus()
    try:
        await _exec(adapter, "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        introspector = Introspector(adapter, bus)
        snapshot = await introspector.snapshot(lazy=True)
        obj = snapshot.schemas["main"][0]
        assert obj.columns == []

        columns = await introspector.fetch_columns("main", "t")
        indexes = await introspector.fetch_indexes("main", "t")
        fks = await introspector.fetch_foreign_keys("main", "t")
        introspector.update_object("main", "t", columns, fks, indexes)

        cached = (await introspector.snapshot(lazy=True)).schemas["main"][0]
        assert [c.name for c in cached.columns] == ["id", "name"]
        assert cached.indexes == indexes
        assert cached.foreign_keys == fks
    finally:
        await bus.close()
        await adapter.close()


async def test_update_object_ignores_an_unknown_schema_or_object() -> None:
    """A stale tree node must not resurrect a dropped object into the cache."""
    adapter = SQLiteAdapter()
    await adapter.connect(Dsn.parse("sqlite:///:memory:"))
    bus = EventBus()
    try:
        await _exec(adapter, "CREATE TABLE t (id INTEGER)")
        introspector = Introspector(adapter, bus)
        before = await introspector.snapshot(lazy=True)

        introspector.update_object("no_such_schema", "t", [], [], [])
        introspector.update_object("main", "no_such_table", [], [], [])

        assert await introspector.snapshot(lazy=True) is before
    finally:
        await bus.close()
        await adapter.close()


async def test_update_object_before_any_snapshot_is_a_no_op() -> None:
    adapter = SQLiteAdapter()
    await adapter.connect(Dsn.parse("sqlite:///:memory:"))
    bus = EventBus()
    try:
        introspector = Introspector(adapter, bus)
        introspector.update_object("main", "t", [], [], [])  # must not raise
    finally:
        await bus.close()
        await adapter.close()
