from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from registro_adapters.sqlite_adapter import SQLiteAdapter
from registro_core.errors import QueryCancelledError
from registro_core.models.connection import Dsn

if TYPE_CHECKING:
    import pathlib


@pytest.mark.asyncio
async def test_connect_ping_close() -> None:
    a = SQLiteAdapter()
    await a.connect(Dsn.parse("sqlite:///:memory:"))
    assert await a.ping() is True
    await a.close()


@pytest.mark.asyncio
async def test_execute_and_stream() -> None:
    a = SQLiteAdapter()
    await a.connect(Dsn.parse("sqlite:///:memory:"))
    try:
        await a.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        h = await a.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
        async for _ in await a.stream(h):
            pass
        h = await a.execute("SELECT id, name FROM t ORDER BY id")
        rows: list[tuple[Any, ...]] = []
        cols: list[str] = []
        async for batch in await a.stream(h, batch_size=10):
            cols = [c.name for c in batch.columns] or cols
            rows.extend(batch.rows)
        assert cols == ["id", "name"]
        # last batch may be empty terminator
        rows = [r for r in rows if r]
        assert rows == [(1, "a"), (2, "b")]
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_introspect_tables_and_indexes() -> None:
    a = SQLiteAdapter()
    await a.connect(Dsn.parse("sqlite:///:memory:"))
    try:
        h = await a.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE)"
        )
        async for _ in await a.stream(h):
            pass
        h = await a.execute("CREATE INDEX ix_email ON users(email)")
        async for _ in await a.stream(h):
            pass
        snap = await a.introspect()
        assert "main" in snap.schemas
        users = next(o for o in snap.schemas["main"] if o.name == "users")
        col_names = [c.name for c in users.columns]
        assert "id" in col_names
        assert "email" in col_names
        assert any(i.name == "ix_email" for i in users.indexes)
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_introspect_handles_quoted_identifiers() -> None:
    a = SQLiteAdapter()
    await a.connect(Dsn.parse("sqlite:///:memory:"))
    try:
        # Table name contains both a single quote and a double quote — legal in
        # SQLite when the CREATE statement quotes the identifier with doubled "".
        h = await a.execute('CREATE TABLE "weird""name\'with-quotes" (id INTEGER, val TEXT)')
        async for _ in await a.stream(h):
            pass
        h = await a.execute('CREATE INDEX "ix_weird\'name" ON "weird""name\'with-quotes"(val)')
        async for _ in await a.stream(h):
            pass
        snap = await a.introspect()
        target = next(o for o in snap.schemas["main"] if o.name == "weird\"name'with-quotes")
        col_names = [c.name for c in target.columns]
        assert col_names == ["id", "val"]
        assert any(i.name == "ix_weird'name" for i in target.indexes)
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_cancel_sets_status() -> None:
    a = SQLiteAdapter()
    await a.connect(Dsn.parse("sqlite:///:memory:"))
    try:
        h = await a.execute("SELECT 1")
        await a.cancel(h)
        # consume stream — should raise cancelled
        with pytest.raises(QueryCancelledError):
            async for _ in await a.stream(h):
                pass
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_sqlite_dml_persistence(tmp_path: pathlib.Path) -> None:

    db_file = tmp_path / "test.db"
    dsn = Dsn.parse(f"sqlite:///{db_file}")

    # First connection: create table and insert data
    a1 = SQLiteAdapter()
    await a1.connect(dsn)
    try:
        h1 = await a1.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        async for _ in await a1.stream(h1):
            pass
        h2 = await a1.execute("INSERT INTO users (id, name) VALUES (1, 'Alice')")
        async for _ in await a1.stream(h2):
            pass
    finally:
        await a1.close()

    # Second connection: verify the data is persisted and readable
    a2 = SQLiteAdapter()
    await a2.connect(dsn)
    try:
        h3 = await a2.execute("SELECT id, name FROM users")
        rows = []
        async for batch in await a2.stream(h3):
            rows.extend(batch.rows)
        # Filter out empty terminator batches
        rows = [r for r in rows if r]
        assert rows == [(1, "Alice")]
    finally:
        await a2.close()


@pytest.mark.asyncio
async def test_failed_do_connect_closes_the_driver_connection() -> None:
    """A connection that half-opens must not be stranded.

    ``connect()`` only sets ``_connected`` after ``_do_connect`` returns, and
    ``close()`` is a no-op while that flag is False. Since ``_do_connect``
    opens the aiosqlite connection and *then* runs PRAGMAs, a failure in the
    second half used to leak the connection and its worker thread.
    """
    a = SQLiteAdapter()
    original = a._do_connect

    async def _boom(dsn: Dsn) -> None:
        await original(dsn)  # the real driver connection is opened here
        raise RuntimeError("PRAGMA failed")

    a._do_connect = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="PRAGMA failed"):
        await a.connect(Dsn.parse("sqlite:///:memory:"))

    assert a._connected is False
    assert a._conn is None, "the half-open driver connection was not released"
