"""Bounded asyncio connection pool.

Adapters are cheap to construct but expensive to connect. The pool reuses
connected adapter instances across queries and bounds the concurrency.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from registro_core.logging import get_logger
from registro_core.models.connection import Dsn
from registro_core.protocols.adapter import DatabaseAdapter

logger = get_logger(__name__)

AdapterFactory = Callable[[Dsn], Awaitable[DatabaseAdapter]]

# How long ``close()`` waits for in-flight release/cleanup tasks before giving
# up on them. Long enough for a normal reset+close, short enough that a wedged
# connection cannot hang application shutdown.
_CLEANUP_TIMEOUT = 5.0


class ConnectionPool:
    """Bounded pool of pre-connected adapter instances for a single DSN."""

    def __init__(self, dsn: Dsn, factory: AdapterFactory, *, size: int = 5) -> None:
        if size < 1:
            raise ValueError("pool size must be >= 1")
        self._dsn = dsn
        self._factory = factory
        self._size = size
        self._idle: asyncio.LifoQueue[DatabaseAdapter] = asyncio.LifoQueue(maxsize=size)
        self._sem = asyncio.Semaphore(size)
        self._all: list[DatabaseAdapter] = []
        self._closed = False
        self._lock = asyncio.Lock()
        self._waiters: set[asyncio.Future[DatabaseAdapter]] = set()
        self._connection_tasks: set[asyncio.Task[Any]] = set()
        self._cleanup_tasks: set[asyncio.Task[Any]] = set()

    async def preload(self, adapter: DatabaseAdapter) -> None:
        """Adopt an already-connected *adapter* as an idle pool member.

        Used when the caller had to build the connection itself — an in-memory
        DuckDB database cannot be reached by a second connection, so the pool
        must be handed the one that exists rather than creating its own.
        """
        async with self._lock:
            if self._closed:
                raise RuntimeError("pool is closed")
            if len(self._all) >= self._size:
                raise ValueError("pool is already at capacity")
            self._all.append(adapter)
            self._idle.put_nowait(adapter)

    async def _acquire(self) -> DatabaseAdapter:
        if self._closed:
            raise RuntimeError("pool is closed")
        await self._sem.acquire()
        # From here on, any failure/cancellation path must release the semaphore
        # (or delegate its release to _release if we got an adapter).
        adapter_to_release: DatabaseAdapter | None = None
        sem_needs_release = True
        try:
            try:
                adapter = self._idle.get_nowait()
                adapter_to_release = adapter
                sem_needs_release = False
                return adapter
            except asyncio.QueueEmpty:
                pass
            # Decide under the lock so ``close()`` cannot race the waiter set:
            # either build a new adapter or register a tracked waiter.
            task: asyncio.Task[DatabaseAdapter] | None = None
            async with self._lock:
                if self._closed:
                    raise RuntimeError("pool is closed")
                if len(self._all) + len(self._connection_tasks) < self._size:
                    # Start connection task in the background. If cancelled,
                    # schedule background cleanup to register/release the adapter.
                    async def _make() -> DatabaseAdapter:
                        return await self._factory(self._dsn)

                    task = asyncio.create_task(_make())
                    self._connection_tasks.add(task)
                    task.add_done_callback(self._connection_tasks.discard)
                else:
                    # Someone else created/is creating one — wait for an idle adapter.
                    # Track the waiter so ``close()`` can wake it instead of hanging it.
                    getter: asyncio.Future[DatabaseAdapter] = asyncio.ensure_future(
                        self._idle.get()
                    )
                    self._waiters.add(getter)

            if task is not None:
                try:
                    adapter = await asyncio.shield(task)
                    async with self._lock:
                        self._all.append(adapter)
                    adapter_to_release = adapter
                    sem_needs_release = False
                    return adapter
                except asyncio.CancelledError:
                    sem_needs_release = False

                    async def _cleanup() -> None:
                        try:
                            a = await task
                            async with self._lock:
                                self._all.append(a)
                            await self._release(a)
                        except BaseException:
                            # ``BaseException``, not ``Exception``: ``close()``
                            # cancels this very task, and the resulting
                            # ``CancelledError`` used to skip the release,
                            # losing the permit for the life of the pool.
                            self._sem.release()
                            raise

                    cleanup_task = asyncio.create_task(_cleanup())
                    self._cleanup_tasks.add(cleanup_task)
                    cleanup_task.add_done_callback(self._cleanup_tasks.discard)
                    raise
            try:
                adapter = await getter
                adapter_to_release = adapter
                sem_needs_release = False
                return adapter
            except asyncio.CancelledError:
                if not getter.done():
                    getter.cancel()
                elif not getter.cancelled() and getter.exception() is None:
                    adapter_to_release = getter.result()
                    sem_needs_release = False
                raise
            finally:
                self._waiters.discard(getter)
        except BaseException:
            if adapter_to_release is not None:
                rel_task = asyncio.create_task(self._release(adapter_to_release))
                self._cleanup_tasks.add(rel_task)
                rel_task.add_done_callback(self._cleanup_tasks.discard)
            elif sem_needs_release:
                self._sem.release()
            raise

    async def _release(self, adapter: DatabaseAdapter) -> None:
        async def _do_release() -> None:
            try:
                await adapter.reset()
            except Exception:
                logger.exception("pool_release_reset_failed")
            finally:
                async with self._lock:
                    if self._closed:
                        # ``close()`` empties ``_all``, so an adapter that was
                        # checked out at that moment is no longer listed. Close
                        # it regardless — testing membership first would leak
                        # exactly the connections that were busy at shutdown.
                        if adapter in self._all:
                            self._all.remove(adapter)
                        with contextlib.suppress(Exception):
                            await adapter.close()
                        self._sem.release()
                    else:
                        # ``put_nowait`` rather than ``put``: the queue is sized
                        # to the pool, so a blocking put under the lock could
                        # only ever deadlock.
                        self._idle.put_nowait(adapter)
                        self._sem.release()

        # Shield the cleanup so that even if the outer task gets cancelled,
        # the semaphore and adapter are safely returned/released.
        await asyncio.shield(_do_release())

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[DatabaseAdapter]:
        adapter = await self._acquire()
        try:
            yield adapter
        finally:
            await self._release(adapter)

    async def close(self) -> None:
        # Hold the lock so a concurrent ``_acquire`` either observes ``_closed``
        # before registering a waiter or is woken by the cancellation below.
        async with self._lock:
            self._closed = True
            for getter in self._waiters:
                getter.cancel()
            self._waiters.clear()
            for t in list(self._connection_tasks):
                t.cancel()
            self._connection_tasks.clear()
            # We do NOT cancel self._cleanup_tasks, let them run to completion
            # so they can release/close any completed connection.
            pending_cleanup = list(self._cleanup_tasks)
            adapters = list(self._all)
            self._all.clear()
        for adapter in adapters:
            try:
                await adapter.close()
            except Exception:
                logger.exception("pool_close_adapter_failed")
        if pending_cleanup:
            # Await them so the loop does not shut down underneath a pending
            # task ("Task was destroyed but it is pending"). Bounded, because a
            # wedged cleanup must not hang application shutdown.
            done, still_pending = await asyncio.wait(pending_cleanup, timeout=_CLEANUP_TIMEOUT)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    logger.warning("pool_cleanup_task_failed", error=str(task.exception()))
            for task in still_pending:
                task.cancel()
