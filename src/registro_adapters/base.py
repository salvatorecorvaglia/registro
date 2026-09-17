"""Abstract base class shared by all adapters.

Provides:
* DSN holding + lifecycle bookkeeping
* Cancellation flag plumbing (per-handle)
* Default introspection (subclasses can override with native catalog queries)
"""

from __future__ import annotations

import asyncio
import contextlib
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn, TypeVar

from registro_core.errors import AdapterError, QueryCancelledError, QueryError
from registro_core.logging import get_logger
from registro_core.models.query import QueryHandle, QueryMetadata, QueryStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from registro_core.models.connection import Dsn
    from registro_core.models.result import ResultBatch
    from registro_core.models.schema import (
        ForeignKey,
        Index,
        SchemaSnapshot,
        TableColumn,
    )
    from registro_core.protocols.adapter import Transaction

logger = get_logger(__name__)

_ConnT = TypeVar("_ConnT")


def connection_id(conn: object) -> str:
    """Stable per-process identifier for a driver connection object.

    Used for ``QueryHandle.connection_id`` / ``SchemaSnapshot.connection_id``.
    The value only needs to be stable for the connection's lifetime and
    distinct between live connections — ``id()`` satisfies both.
    """
    return hex(id(conn))


class _CancelledStream:
    """Async iterator that reports a cancelled query on its first iteration.

    ``stream()`` has to hand back an iterator, so a query cancelled *before*
    streaming began cannot simply raise from ``stream()`` itself without
    changing the contract every caller is written against. Adapters previously
    each hand-rolled a generator with an always-true ``if`` guarding an
    unreachable ``yield`` — the ``yield`` existing only to make the function a
    generator. This says the same thing without the dead branch.
    """

    __slots__ = ("_handle",)

    def __init__(self, handle: QueryHandle) -> None:
        self._handle = handle

    def __aiter__(self) -> _CancelledStream:
        return self

    async def __anext__(self) -> ResultBatch:
        self._handle.mark_done(QueryStatus.CANCELLED)
        raise QueryCancelledError("query cancelled", context={"query_id": self._handle.query_id})


class AbstractAdapter(ABC):
    """Skeleton implementation of :class:`DatabaseAdapter`.

    Threading/concurrency contract: an adapter instance is **single-owner** —
    it serves one query at a time. The engine enforces this by handing every
    run a dedicated adapter from :class:`~registro_engine.pool.ConnectionPool`.
    The per-handle ``_cancel_flags`` / ``_metadata`` dicts are therefore only
    mutated from one logical caller; they are not guarded for concurrent
    ``execute`` calls on the same instance.
    """

    name: ClassVar[str] = "abstract"
    schemes: ClassVar[tuple[str, ...]] = ()

    def __init__(self) -> None:
        self._dsn: Dsn | None = None
        self._connected = False
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._metadata: dict[str, QueryMetadata] = {}

    # ---- lifecycle ----------------------------------------------------

    @property
    def dsn(self) -> Dsn:
        if self._dsn is None:
            raise AdapterError(
                f"{self.name}: not connected",
                context={"adapter": self.name},
            )
        return self._dsn

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self, dsn: Dsn) -> None:
        self._dsn = dsn
        try:
            await self._do_connect(dsn)
        except BaseException:
            # Every adapter opens the driver connection and *then* does more
            # work that can fail: PRAGMAs on SQLite, ``pg_backend_pid()`` on
            # Postgres, ``CONNECTION_ID()`` on MySQL. ``close()`` is a no-op
            # while ``_connected`` is False, so without this unwind the
            # half-open connection — and, for aiosqlite, its worker thread —
            # would never be released.
            with contextlib.suppress(Exception):
                await self._do_close()
            raise
        self._connected = True
        logger.info("adapter_connected", adapter=self.name, scheme=dsn.scheme)

    async def close(self) -> None:
        if not self._connected:
            return
        try:
            await self._do_close()
        finally:
            self._connected = False
            self._cancel_flags.clear()
            self._metadata.clear()
            logger.info("adapter_closed", adapter=self.name)

    async def __aenter__(self) -> AbstractAdapter:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    @abstractmethod
    async def _do_connect(self, dsn: Dsn) -> None: ...

    @abstractmethod
    async def _do_close(self) -> None: ...

    @abstractmethod
    async def ping(self) -> bool: ...

    # ---- execution ----------------------------------------------------

    @abstractmethod
    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> QueryHandle: ...

    @abstractmethod
    async def stream(
        self, handle: QueryHandle, *, batch_size: int = 1000
    ) -> AsyncIterator[ResultBatch]: ...

    async def metadata(self, handle: QueryHandle) -> QueryMetadata:
        return self._metadata.get(handle.query_id, QueryMetadata())

    async def cancel(self, handle: QueryHandle) -> None:
        # Create-or-get — cancel() must be effective even if stream() hasn't
        # been called yet for this handle (the executor may cancel between
        # execute and stream).
        self._cancel_event(handle).set()
        handle.mark_done(QueryStatus.CANCELLED)
        logger.info("adapter_cancel_requested", adapter=self.name, query_id=handle.query_id)

    def _require_conn(self, conn: _ConnT | None) -> _ConnT:
        """Narrow a nullable driver-connection attribute to non-None.

        Every adapter method needs its live connection before doing
        anything; centralizing the check keeps the "not connected" message
        and error type consistent across all adapters instead of each
        subclass repeating ``if self._conn is None: raise AdapterError(...)``.
        """
        if conn is None:
            raise AdapterError(f"{self.name}: not connected")
        return conn

    def _wrap_driver_error(
        self,
        exc: Exception,
        handle: QueryHandle,
        *,
        sql: str | None = None,
        mark_failed: bool = True,
    ) -> NoReturn:
        """Translate a driver-native error into the Registro error hierarchy.

        Checks the handle's cancel flag first: a query cancelled mid-flight
        surfaces as :class:`QueryCancelledError` regardless of which
        exception the driver happened to raise to unblock the cancelled
        call — from the caller's perspective, the cancellation is what
        matters, not the driver's internal plumbing.

        ``mark_failed=False`` matches the streaming loops, which leave the
        handle's status alone on a non-cancel error (only the initial
        ``execute()`` call transitions it to ``FAILED``).
        """
        if self._cancel_event(handle).is_set():
            handle.mark_done(QueryStatus.CANCELLED)
            raise QueryCancelledError(
                "query cancelled", context={"query_id": handle.query_id}
            ) from exc
        if mark_failed:
            handle.mark_done(QueryStatus.FAILED)
        raise QueryError(str(exc), context={"sql": sql if sql is not None else handle.sql}) from exc

    def _cancelled_stream(self, handle: QueryHandle) -> AsyncIterator[ResultBatch]:
        """Return a stream that raises :class:`QueryCancelledError` when iterated."""
        return _CancelledStream(handle)

    def _cancel_event(self, handle: QueryHandle) -> asyncio.Event:
        flag = self._cancel_flags.get(handle.query_id)
        if flag is None:
            flag = asyncio.Event()
            self._cancel_flags[handle.query_id] = flag
        return flag

    def _set_metadata(self, handle: QueryHandle, meta: QueryMetadata) -> None:
        # An adapter serves one query at a time, and callers read metadata
        # *after* draining the stream (row counts, server messages). Keeping
        # only the latest entry makes it available then without the dict
        # growing once per query for the life of the connection.
        self._metadata.clear()
        self._metadata[handle.query_id] = meta

    def _forget(self, handle: QueryHandle) -> None:
        """Drop per-query state once its stream is finished.

        Metadata deliberately survives: it describes the completed result and
        is what the UI renders after the last batch. It is bounded by
        :meth:`_set_metadata` and cleared by :meth:`reset`.
        """
        self._cancel_flags.pop(handle.query_id, None)

    async def reset(self) -> None:
        """Return the adapter to a clean state before it is reused.

        Called by :class:`~registro_engine.pool.ConnectionPool` on release.
        Subclasses **must** also unwind connection-level state — an open
        transaction, a server-side cursor — because the next caller inherits
        this connection and would otherwise run inside someone else's
        transaction.
        """
        self._cancel_flags.clear()
        self._metadata.clear()

    # ---- transactions / introspection --------------------------------

    @abstractmethod
    async def begin(self) -> Transaction: ...

    @abstractmethod
    async def introspect(self, *, lazy: bool = False) -> SchemaSnapshot: ...

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        return []

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        return []

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        return []
