from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from registro_adapters.sqlite_adapter import SQLiteAdapter
from registro_core.models.connection import Dsn
from registro_engine.pool import ConnectionPool

if TYPE_CHECKING:
    from registro_core.protocols.adapter import DatabaseAdapter

_MEM = "sqlite:///:memory:"


async def _factory(dsn: Dsn) -> DatabaseAdapter:
    a = SQLiteAdapter()
    await a.connect(dsn)
    return a


@pytest.mark.asyncio
async def test_pool_reuses_connection() -> None:
    pool = ConnectionPool(Dsn.parse(_MEM), _factory, size=2)
    try:
        async with pool.acquire() as a1:
            assert await a1.ping()
        async with pool.acquire() as a2:
            assert await a2.ping()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_factory_failure_does_not_leak_semaphore_slot() -> None:
    """A failed adapter factory must release the slot it tentatively held.

    Before the fix, ``size`` consecutive factory failures permanently drained
    the semaphore and every later ``acquire()`` hung forever.
    """
    calls = {"n": 0}

    async def flaky(dsn: Dsn) -> DatabaseAdapter:
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("connect failed")
        return await _factory(dsn)

    pool = ConnectionPool(Dsn.parse(_MEM), flaky, size=1)
    try:
        for _ in range(3):
            with pytest.raises(RuntimeError, match="connect failed"):
                async with pool.acquire():
                    pass
        # Would hang forever if the slot had leaked.
        async with asyncio.timeout(2):
            async with pool.acquire() as adapter:
                assert await adapter.ping()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_acquire_after_close_raises() -> None:
    pool = ConnectionPool(Dsn.parse(_MEM), _factory, size=1)
    await pool.close()
    with pytest.raises(RuntimeError, match="closed"):
        async with pool.acquire():
            pass


@pytest.mark.asyncio
async def test_pool_acquire_cancellation_safety() -> None:
    """Acquisition and release must not leak slots or adapters under cancellation."""
    pool = ConnectionPool(Dsn.parse(_MEM), _factory, size=1)
    try:
        async with pool.acquire() as a1:
            assert await a1.ping()

            waiting = asyncio.Event()

            async def try_acquire() -> None:
                # The pool is at capacity, so this parks on the waiter queue.
                waiting.set()
                async with pool.acquire():
                    pass

            task = asyncio.create_task(try_acquire())
            # Wait for the task to have actually started, rather than sleeping
            # a guessed interval and hoping. The pool is at capacity, so it then
            # blocks inside ``_acquire`` on the semaphore; one loop turn is
            # enough to get it there. (Not a ``_waiters`` check: at size 1 the
            # semaphore blocks *before* a waiter is ever registered, so that
            # condition would never become true.)
            await asyncio.wait_for(waiting.wait(), timeout=5)
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        async with asyncio.timeout(1):
            async with pool.acquire() as a2:
                assert await a2.ping()
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_pool_close_cancellation_safety_does_not_leak_adapter() -> None:
    """Closing the pool cancels connection tasks but lets cleanup finish to avoid leaks."""
    connections_created = []
    # Events rather than wall-clock sleeps: the old version slept 0.03s hoping
    # the connection had been created and 0.05s hoping cleanup had finished,
    # which is a race dressed as a delay. These say what is actually awaited.
    connected = asyncio.Event()
    holding = asyncio.Event()

    async def tracking_factory(dsn: Dsn) -> DatabaseAdapter:
        a = SQLiteAdapter()
        await a.connect(dsn)
        connections_created.append(a)
        connected.set()
        return a

    pool = ConnectionPool(Dsn.parse(_MEM), tracking_factory, size=1)

    async def try_acquire() -> None:
        async with pool.acquire():
            holding.set()
            await asyncio.Event().wait()  # hold the adapter until cancelled

    task = asyncio.create_task(try_acquire())
    await asyncio.wait_for(connected.wait(), timeout=5)
    await asyncio.wait_for(holding.wait(), timeout=5)

    # Cancelling the holder triggers the pool's cleanup/release path.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await pool.close()

    assert len(connections_created) > 0
    for a in connections_created:
        assert a.connected is False, "close() left a tracked connection open"


@pytest.mark.asyncio
async def test_close_closes_an_adapter_that_was_checked_out() -> None:
    """Closing the pool must not leak the connections that were in use.

    ``close()`` empties ``_all``, so the release path could no longer find the
    checked-out adapter in it and skipped closing that connection entirely.
    """
    pool = ConnectionPool(Dsn.parse(_MEM), _factory, size=2)
    released: list[DatabaseAdapter] = []
    acquired = asyncio.Event()
    let_go = asyncio.Event()

    async def _hold() -> None:
        async with pool.acquire() as adapter:
            released.append(adapter)
            acquired.set()
            await let_go.wait()

    holder = asyncio.create_task(_hold())
    await asyncio.wait_for(acquired.wait(), timeout=5)

    # Close *while* the adapter is still checked out — that is the case under
    # test — and only then let the holder unwind. Its release will find the
    # connection already closed and log a harmless reset failure.
    await asyncio.wait_for(pool.close(), timeout=5)
    let_go.set()
    await asyncio.wait_for(holder, timeout=5)

    assert not released[0].connected, "checked-out adapter was left open"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_preload_adopts_an_existing_connection() -> None:
    pool = ConnectionPool(Dsn.parse(_MEM), _factory, size=1)
    seeded = await _factory(Dsn.parse(_MEM))
    await pool.preload(seeded)
    try:
        async with pool.acquire() as adapter:
            assert adapter is seeded
        # Still usable after being returned.
        async with pool.acquire() as adapter:
            assert adapter is seeded
    finally:
        await pool.close()
    assert not seeded.connected  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_preload_refuses_to_exceed_capacity() -> None:
    pool = ConnectionPool(Dsn.parse(_MEM), _factory, size=1)
    await pool.preload(await _factory(Dsn.parse(_MEM)))
    try:
        with pytest.raises(ValueError, match="capacity"):
            await pool.preload(await _factory(Dsn.parse(_MEM)))
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_release_rolls_back_a_transaction_left_open() -> None:
    """A connection must never return to the pool inside a transaction."""
    pool = ConnectionPool(Dsn.parse(_MEM), _factory, size=1)
    try:
        async with pool.acquire() as adapter:
            await adapter.execute("BEGIN")
            assert adapter._conn.in_transaction  # type: ignore[attr-defined]
            leaked = adapter
        assert not leaked._conn.in_transaction, (  # type: ignore[attr-defined]
            "adapter went back to the pool still in a transaction"
        )
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_close_returns_the_permit_of_a_cancelled_connection_task() -> None:
    """``close()`` cancelling an in-flight connect must not consume a permit.

    ``_acquire`` takes the semaphore, then delegates the release to a background
    ``_cleanup`` task when the acquirer is cancelled. That task awaited the
    connection task and caught only ``Exception`` — but ``close()`` *cancels*
    that connection task, and ``CancelledError`` is a ``BaseException``, so the
    release was skipped and the permit was lost for the life of the pool.
    """
    connecting = asyncio.Event()

    async def never_completes(dsn: Dsn) -> DatabaseAdapter:
        connecting.set()
        await asyncio.Event().wait()  # never resolves
        raise AssertionError("unreachable")

    pool = ConnectionPool(Dsn.parse(_MEM), never_completes, size=1)
    acquiring = asyncio.create_task(pool._acquire())
    await asyncio.wait_for(connecting.wait(), timeout=1)

    await pool.close()
    acquiring.cancel()
    with pytest.raises((asyncio.CancelledError, RuntimeError)):
        await acquiring

    # Let the background cleanup task run to completion.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert pool._sem._value == 1, "pool permit was not returned after close()"
