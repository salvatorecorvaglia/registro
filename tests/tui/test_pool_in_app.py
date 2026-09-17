"""TUI wires a ConnectionPool into the app and uses it for query execution."""

from __future__ import annotations

import asyncio

import pytest

textual = pytest.importorskip("textual")

from registro_core.config import RegistroConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_engine.executor import QueryExecutor  # noqa: E402
from registro_engine.pool import ConnectionPool  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402


@pytest.mark.asyncio
async def test_connect_populates_pool() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert isinstance(app.state.pool, ConnectionPool)
        assert app.state.adapter is not None

        # The introspection adapter must be its own connection, so a long
        # SELECT holding a pool slot cannot corrupt a schema refresh.
        async with app.state.pool.acquire() as pooled_adapter:
            assert app.state.adapter is not pooled_adapter


@pytest.mark.asyncio
async def test_in_memory_sqlite_is_shared_across_connections() -> None:
    """Separate connections to ``sqlite:///:memory:`` must see one database.

    The DSN is rewritten to a named shared-cache URI at connect time; without
    that, the introspection connection would open its own empty database and
    the schema tree would never show a table the user just created.
    """
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        pool = app.state.pool
        introspect_adapter = app.state.adapter
        assert pool is not None
        assert introspect_adapter is not None

        async with pool.acquire() as adapter:
            handle = await adapter.execute("CREATE TABLE shared_demo (id INTEGER)")
            async for _ in await adapter.stream(handle):
                pass

        snapshot = await introspect_adapter.introspect()
        names = [obj.name for objects in snapshot.schemas.values() for obj in objects]
        assert "shared_demo" in names


@pytest.mark.asyncio
async def test_pool_serves_two_concurrent_queries() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        pool = app.state.pool
        assert pool is not None

        async def run_one() -> int:
            async with pool.acquire() as adapter:
                executor = QueryExecutor(adapter, app.state.bus)
                run = await executor.run("SELECT 1 AS a")
                emitted = 0
                async for batch in run.rows:
                    emitted += len(batch.rows)
                return emitted

        results = await asyncio.gather(run_one(), run_one())
        assert list(results) == [1, 1]


@pytest.mark.asyncio
async def test_reconnecting_closes_the_previous_session() -> None:
    """Two connects in a row must not orphan the first one's connections.

    ``_connect`` used to overwrite ``state.*`` outright, so a second connect
    left the first session's adapter, pool and history database open with
    nothing holding a reference to close them.
    """
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        first_adapter = app.state.adapter
        first_pool = app.state.pool
        assert first_adapter is not None

        await app._connect(Dsn.parse("sqlite:///:memory:"))
        await pilot.pause()

        assert app.state.adapter is not first_adapter, "session was not replaced"
        assert not first_adapter.connected, "previous adapter was left open"  # type: ignore[attr-defined]
        assert first_pool is not None
        assert first_pool._closed, "previous pool was left open"


@pytest.mark.asyncio
async def test_concurrent_connects_do_not_race() -> None:
    """The connect lock serializes them, leaving exactly one live session."""
    app = RegistroApp(config=RegistroConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        # Dismiss the connection dialog the app opens when it has no DSN.
        app.pop_screen()
        await pilot.pause()

        await asyncio.gather(
            app._connect(Dsn.parse("sqlite:///:memory:")),
            app._connect(Dsn.parse("sqlite:///:memory:")),
        )
        await pilot.pause()

        assert app.state.adapter is not None
        assert app.state.adapter.connected  # type: ignore[attr-defined]
        async with app.state.pool.acquire() as adapter:  # type: ignore[union-attr]
            assert await adapter.ping()
