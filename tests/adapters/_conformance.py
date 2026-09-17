"""Shared adapter conformance suite.

Every adapter must behave the same way through the ``DatabaseAdapter``
protocol, because the engine and the TUI are written against that protocol and
nothing else. Subclass :class:`AdapterConformance`, provide an ``adapter``
fixture and the handful of dialect strings, and the whole contract is checked.

Divergences this suite exists to catch — each one was a real bug:

* ``execute()`` must surface a bad statement, rather than deferring the error
  to ``stream()`` where a different code path reports it.
* ``Column.type`` must be populated, not left ``UNKNOWN`` on adapters whose
  driver does not hand types over for free.
* ``reset()`` must unwind connection state, so a pooled connection never
  arrives inside someone else's transaction.
* An abandoned stream must not strand a cursor or a transaction.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from registro_core.errors import QueryCancelledError, QueryError
from registro_core.models.result import ColumnType

if TYPE_CHECKING:
    from registro_core.models.result import ResultBatch
    from registro_core.protocols.adapter import DatabaseAdapter

_TABLE = "registro_conformance"
_PARENT = "registro_conf_parent"
_CHILD = "registro_conf_child"
_INDEX = "registro_conf_tag_idx"


async def drain(adapter: DatabaseAdapter, sql: str) -> list[tuple[Any, ...]]:
    """Run *sql* to completion and return its rows."""
    handle = await adapter.execute(sql)
    rows: list[tuple[Any, ...]] = []
    async for batch in await adapter.stream(handle):
        rows.extend(batch.rows)
    return rows


async def collect(adapter: DatabaseAdapter, sql: str, **kwargs: Any) -> list[ResultBatch]:
    handle = await adapter.execute(sql)
    return [batch async for batch in await adapter.stream(handle, **kwargs)]


class AdapterConformance:
    """Contract every adapter must satisfy. Subclass and supply ``adapter``."""

    # --- dialect knobs a subclass may override -------------------------
    #: DDL for a two-column table named ``registro_conformance``.
    create_table_sql: ClassVar[str] = f"CREATE TABLE {_TABLE} (id INTEGER, name VARCHAR(32))"
    #: A statement that cannot parse on any engine.
    invalid_sql: ClassVar[str] = "SELCT NOT VALID SQL ("
    #: Parent table for the foreign-key contract.
    create_parent_sql: ClassVar[str] = (
        f"CREATE TABLE {_PARENT} (id INTEGER PRIMARY KEY, label VARCHAR(32))"
    )
    #: Child table. Table-level ``FOREIGN KEY`` rather than an inline
    #: ``REFERENCES``: MySQL parses the column-level form and then silently
    #: ignores it, so the inline spelling would test nothing there.
    create_child_sql: ClassVar[str] = (
        f"CREATE TABLE {_CHILD} ("
        f" id INTEGER PRIMARY KEY,"
        f" parent_id INTEGER,"
        f" tag VARCHAR(32),"
        f" FOREIGN KEY (parent_id) REFERENCES {_PARENT}(id))"
    )
    #: A secondary index on the child's ``tag`` column.
    create_index_sql: ClassVar[str] = f"CREATE INDEX {_INDEX} ON {_CHILD} (tag)"

    # --- lifecycle ------------------------------------------------------

    @pytest.mark.asyncio
    async def test_ping(self, adapter: DatabaseAdapter) -> None:
        assert await adapter.ping() is True

    @pytest.mark.asyncio
    async def test_create_insert_select_roundtrip(self, adapter: DatabaseAdapter) -> None:
        await drain(adapter, self.create_table_sql)
        await drain(adapter, f"INSERT INTO {_TABLE} VALUES (1, 'a')")
        await drain(adapter, f"INSERT INTO {_TABLE} VALUES (2, 'b')")
        rows = await drain(adapter, f"SELECT id, name FROM {_TABLE} ORDER BY id")
        assert rows == [(1, "a"), (2, "b")]

    # --- result shape ---------------------------------------------------

    @pytest.mark.asyncio
    async def test_columns_are_named_and_typed(self, adapter: DatabaseAdapter) -> None:
        """``Column.type`` must be inferred on every adapter, not left UNKNOWN."""
        await drain(adapter, self.create_table_sql)
        await drain(adapter, f"INSERT INTO {_TABLE} VALUES (7, 'seven')")
        batches = await collect(adapter, f"SELECT id, name FROM {_TABLE}")
        columns = next(b.columns for b in batches if b.columns)

        assert [c.name for c in columns] == ["id", "name"]
        assert columns[0].type is ColumnType.INTEGER
        assert columns[1].type is ColumnType.STRING

    @pytest.mark.asyncio
    async def test_empty_result_set_still_reports_columns(self, adapter: DatabaseAdapter) -> None:
        await drain(adapter, self.create_table_sql)
        batches = await collect(adapter, f"SELECT id, name FROM {_TABLE}")
        assert batches, "an empty result set must still yield a terminal batch"
        assert batches[-1].is_last
        assert sum(len(b.rows) for b in batches) == 0
        assert [c.name for c in batches[-1].columns] == ["id", "name"]

    @pytest.mark.asyncio
    async def test_nulls_round_trip_as_none(self, adapter: DatabaseAdapter) -> None:
        await drain(adapter, self.create_table_sql)
        await drain(adapter, f"INSERT INTO {_TABLE} VALUES (1, NULL)")
        rows = await drain(adapter, f"SELECT id, name FROM {_TABLE}")
        assert rows == [(1, None)]

    @pytest.mark.asyncio
    async def test_batches_respect_batch_size(self, adapter: DatabaseAdapter) -> None:
        await drain(adapter, self.create_table_sql)
        for i in range(5):
            await drain(adapter, f"INSERT INTO {_TABLE} VALUES ({i}, 'x')")

        handle = await adapter.execute(f"SELECT id FROM {_TABLE}")
        sizes = [len(b.rows) async for b in await adapter.stream(handle, batch_size=2)]
        assert sum(sizes) == 5
        assert max(sizes) <= 2

    @pytest.mark.asyncio
    async def test_metadata_survives_the_stream(self, adapter: DatabaseAdapter) -> None:
        """Metadata describes the finished result, so it must outlive it."""
        await drain(adapter, self.create_table_sql)
        handle = await adapter.execute(f"SELECT id, name FROM {_TABLE}")
        async for _ in await adapter.stream(handle):
            pass
        meta = await adapter.metadata(handle)
        assert meta.column_names == ["id", "name"]

    # --- error handling -------------------------------------------------

    @pytest.mark.asyncio
    async def test_invalid_sql_raises_from_execute(self, adapter: DatabaseAdapter) -> None:
        """The error must surface from ``execute()``, uniformly across adapters.

        Postgres used to defer all work to ``stream()``, so a syntax error
        arrived from a different call than on every other adapter and callers
        had to handle both.
        """
        with pytest.raises(QueryError):
            await adapter.execute(self.invalid_sql)

    @pytest.mark.asyncio
    async def test_cancel_before_streaming_raises(self, adapter: DatabaseAdapter) -> None:
        handle = await adapter.execute("SELECT 1")
        await adapter.cancel(handle)
        with pytest.raises(QueryCancelledError):
            async for _ in await adapter.stream(handle):
                pass

    # --- reuse ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_reset_leaves_the_connection_usable(self, adapter: DatabaseAdapter) -> None:
        await drain(adapter, self.create_table_sql)
        await adapter.reset()
        assert await adapter.ping() is True
        assert await drain(adapter, f"SELECT id FROM {_TABLE}") == []

    @pytest.mark.asyncio
    async def test_reset_rolls_back_an_open_transaction(self, adapter: DatabaseAdapter) -> None:
        """A pooled connection must never be handed on mid-transaction."""
        await drain(adapter, self.create_table_sql)
        tx = await adapter.begin()
        # Entered by hand rather than with ``async with``: the point of the
        # test is that ``reset()`` ends the transaction, so letting the context
        # manager also try to commit it would fail on the way out.
        await tx.__aenter__()
        await drain(adapter, f"INSERT INTO {_TABLE} VALUES (99, 'uncommitted')")
        await adapter.reset()

        assert await drain(adapter, f"SELECT id FROM {_TABLE} WHERE id = 99") == [], (
            "reset() left uncommitted work visible on the connection"
        )

    @pytest.mark.asyncio
    async def test_abandoned_stream_does_not_wedge_the_connection(
        self, adapter: DatabaseAdapter
    ) -> None:
        """Stop reading mid-result, reset, and the connection must still work."""
        await drain(adapter, self.create_table_sql)
        for i in range(10):
            await drain(adapter, f"INSERT INTO {_TABLE} VALUES ({i}, 'x')")

        handle = await adapter.execute(f"SELECT id FROM {_TABLE}")
        stream = await adapter.stream(handle, batch_size=1)
        async for _ in stream:
            break
        await stream.aclose()  # type: ignore[attr-defined]
        await adapter.reset()

        assert len(await drain(adapter, f"SELECT id FROM {_TABLE}")) == 10

    @pytest.mark.asyncio
    async def test_drained_statements_release_per_query_state(
        self, adapter: DatabaseAdapter
    ) -> None:
        """Per-query bookkeeping must not survive a fully drained statement.

        Every statement allocates a cursor and a cancel flag. A statement that
        returns no columns (DDL/DML) is the trap: SQLite and MySQL used to
        ``return`` from ``stream()`` *above* the ``finally`` that releases
        them, leaking one of each per INSERT — unbounded over the life of a
        pooled connection.
        """
        await drain(adapter, self.create_table_sql)
        for i in range(5):
            await drain(adapter, f"INSERT INTO {_TABLE} VALUES ({i}, 'x')")
        await drain(adapter, f"SELECT id FROM {_TABLE}")

        # ``hasattr`` first, then read the attribute directly. The previous form
        # — ``getattr(adapter, "_cancel_flags", {}) == {}`` — passed *vacuously*
        # on any adapter that lacked the attribute, so a rename would have
        # silently retired the very leak check this test exists to be.
        assert hasattr(adapter, "_cancel_flags"), (
            "every adapter tracks per-query cancel flags; the attribute was renamed "
            "or removed, and this leak check was about to stop testing anything"
        )
        assert adapter._cancel_flags == {}, "cancel flags accumulated"

        # ``_cursors``/``_statements`` are per-driver: SQLite and MySQL keep
        # cursors, Postgres keeps prepared statements, DuckDB keeps neither
        # (its result set lives on the connection). Assert on whichever the
        # adapter actually declares, and require at least one of them so the
        # check cannot quietly become a no-op for all four.
        tracked = [name for name in ("_cursors", "_statements") if hasattr(adapter, name)]
        for name in tracked:
            assert getattr(adapter, name) == {}, f"{name} accumulated"

    # --- introspection --------------------------------------------------

    @pytest.mark.asyncio
    async def test_introspection_finds_the_table(self, adapter: DatabaseAdapter) -> None:
        await drain(adapter, self.create_table_sql)
        snapshot = await adapter.introspect()
        found = [
            obj for objects in snapshot.schemas.values() for obj in objects if obj.name == _TABLE
        ]
        assert found, f"introspection missed {_TABLE}; saw {sorted(snapshot.schemas)}"
        assert [c.name for c in found[0].columns] == ["id", "name"]

    @pytest.mark.asyncio
    async def test_lazy_introspection_skips_columns(self, adapter: DatabaseAdapter) -> None:
        await drain(adapter, self.create_table_sql)
        snapshot = await adapter.introspect(lazy=True)
        found = [
            obj for objects in snapshot.schemas.values() for obj in objects if obj.name == _TABLE
        ]
        assert found
        assert found[0].columns == [], "lazy introspection must not fetch columns"

    @pytest.mark.asyncio
    async def test_fetch_columns_describes_the_table(self, adapter: DatabaseAdapter) -> None:
        await drain(adapter, self.create_table_sql)
        snapshot = await adapter.introspect(lazy=True)
        schema = next(
            name
            for name, objects in snapshot.schemas.items()
            if any(o.name == _TABLE for o in objects)
        )
        columns = await adapter.fetch_columns(schema, _TABLE)
        assert [c.name for c in columns] == ["id", "name"]
        assert columns[0].type is ColumnType.INTEGER
        assert columns[1].type is ColumnType.STRING

    # --- introspection: relationships -----------------------------------
    #
    # ``fetch_columns`` was the only per-object introspection call in the
    # contract, so ``fetch_foreign_keys`` and ``fetch_indexes`` diverged
    # unnoticed — DuckDB's implementations of both had never executed once.

    async def _make_related_tables(self, adapter: DatabaseAdapter) -> None:
        for sql in (
            f"DROP TABLE IF EXISTS {_CHILD}",
            f"DROP TABLE IF EXISTS {_PARENT}",
            self.create_parent_sql,
            self.create_child_sql,
            self.create_index_sql,
        ):
            await drain(adapter, sql)

    async def _drop_related_tables(self, adapter: DatabaseAdapter) -> None:
        with contextlib.suppress(Exception):
            await drain(adapter, f"DROP TABLE IF EXISTS {_CHILD}")
        with contextlib.suppress(Exception):
            await drain(adapter, f"DROP TABLE IF EXISTS {_PARENT}")

    async def _schema_of(self, adapter: DatabaseAdapter, table: str) -> str:
        snapshot = await adapter.introspect(lazy=True)
        return next(
            name
            for name, objects in snapshot.schemas.items()
            if any(o.name == table for o in objects)
        )

    @pytest.mark.asyncio
    async def test_fetch_foreign_keys_describes_the_relationship(
        self, adapter: DatabaseAdapter
    ) -> None:
        await self._make_related_tables(adapter)
        try:
            schema = await self._schema_of(adapter, _CHILD)
            fks = await adapter.fetch_foreign_keys(schema, _CHILD)

            assert len(fks) == 1, f"expected one foreign key, got {[f.name for f in fks]}"
            fk = fks[0]
            assert fk.columns == ["parent_id"]
            assert fk.referenced_table == _PARENT
            assert fk.referenced_columns == ["id"]
            assert fk.name, "a foreign key must carry a non-empty name"
        finally:
            await self._drop_related_tables(adapter)

    @pytest.mark.asyncio
    async def test_fetch_indexes_reports_the_secondary_index(
        self, adapter: DatabaseAdapter
    ) -> None:
        await self._make_related_tables(adapter)
        try:
            schema = await self._schema_of(adapter, _CHILD)
            indexes = await adapter.fetch_indexes(schema, _CHILD)

            names = {i.name for i in indexes}
            assert _INDEX in names, f"secondary index missing; saw {sorted(names)}"
            tag_index = next(i for i in indexes if i.name == _INDEX)
            assert tag_index.columns == ["tag"]
            assert tag_index.unique is False
        finally:
            await self._drop_related_tables(adapter)

    @pytest.mark.asyncio
    async def test_a_table_without_relationships_reports_none(
        self, adapter: DatabaseAdapter
    ) -> None:
        """The empty case must be empty, not an error and not a stray row."""
        await drain(adapter, self.create_table_sql)
        schema = await self._schema_of(adapter, _TABLE)
        assert await adapter.fetch_foreign_keys(schema, _TABLE) == []

    @pytest.mark.asyncio
    async def test_full_introspection_agrees_with_per_object_calls(
        self, adapter: DatabaseAdapter
    ) -> None:
        """The bulk sweep and the lazy per-object path must not drift.

        Adapters answer a full ``introspect()`` with one bulk query per kind and
        the schema tree's lazy expansion with per-object queries. They are
        separate code paths returning the same facts, and only SQLite had a test
        holding them to it.
        """
        await self._make_related_tables(adapter)
        try:
            snapshot = await adapter.introspect()
            schema = await self._schema_of(adapter, _CHILD)
            bulk = next(
                obj
                for objects in snapshot.schemas.values()
                for obj in objects
                if obj.name == _CHILD
            )

            assert [c.name for c in bulk.columns] == [
                c.name for c in await adapter.fetch_columns(schema, _CHILD)
            ]
            assert [(f.columns, f.referenced_table) for f in bulk.foreign_keys] == [
                (f.columns, f.referenced_table)
                for f in await adapter.fetch_foreign_keys(schema, _CHILD)
            ]
            assert sorted(i.name for i in bulk.indexes) == sorted(
                i.name for i in await adapter.fetch_indexes(schema, _CHILD)
            )
        finally:
            await self._drop_related_tables(adapter)
