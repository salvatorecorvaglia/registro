"""High-level query executor.

Wraps a :class:`DatabaseAdapter`, emits lifecycle events on an
:class:`EventBus`, owns per-run cancellation, and surfaces a clean
:class:`QueryRun` handle to the UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from registro_core.errors import QueryCancelledError, QueryError, QueryTimeoutError
from registro_core.events import (
    EventBus,
    QueryCancelled,
    QueryCompleted,
    QueryFailed,
    QueryProgress,
    QueryStarted,
)
from registro_core.logging import bind_context, get_logger, reset_context
from registro_core.models.query import QueryStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from registro_core.models.query import QueryHandle
    from registro_core.models.result import ResultBatch
    from registro_core.protocols.adapter import DatabaseAdapter

logger = get_logger(__name__)

_UNEXPECTED = "registro.query.unexpected"


@dataclass
class QueryRun:
    """Handle to an executing query."""

    handle: QueryHandle
    rows: AsyncIterator[ResultBatch]
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    rows_emitted: int = 0
    _closed: bool = False

    @property
    def query_id(self) -> str:
        return self.handle.query_id

    @property
    def status(self) -> QueryStatus:
        return self.handle.status

    @property
    def duration_ms(self) -> float | None:
        return self.handle.duration_ms

    async def aclose(self) -> None:
        """Finalize the stream, releasing the adapter's cursor/transaction.

        Safe to call after full consumption (a no-op) and safe to call twice.
        Callers that stop iterating early **must** call this: leaving
        finalization to the garbage collector can hand a connection back to the
        pool while it is still inside a transaction.
        """
        if self._closed:
            return
        self._closed = True
        aclose = getattr(self.rows, "aclose", None)
        if aclose is not None:
            await aclose()


class QueryExecutor:
    """Executes SQL against an adapter and broadcasts lifecycle events."""

    def __init__(self, adapter: DatabaseAdapter, event_bus: EventBus) -> None:
        self._adapter = adapter
        self._bus = event_bus

    async def run(
        self,
        sql: str,
        params: Mapping[str, Any] | None = None,
        *,
        batch_size: int = 1000,
        progress_every: int = 5000,
        # ASYNC109 wants callers to wrap the call in ``asyncio.timeout``
        # instead. That cannot work here: the deadline has to bound the
        # *iterator this returns*, which the caller drains long after ``run()``
        # has returned — and expiry has to cancel the query server-side, not
        # merely abandon it.
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> QueryRun:
        """Start execution and return a :class:`QueryRun` whose ``rows`` you iterate.

        The returned iterator emits events as it consumes the stream.

        The caller owns ``run.rows``: iterate it to completion, or call
        ``await run.rows.aclose()`` when abandoning it early. Closing is what
        publishes the terminal lifecycle event and unwinds the adapter's
        cursor/transaction — relying on garbage collection defers both to an
        arbitrary later moment, on an arbitrary task.

        ``timeout`` bounds the *whole* run — execution plus streaming — and
        raises :class:`QueryTimeoutError` from the iterator when it expires.
        ``None`` or a non-positive value disables it. The adapter is asked to
        cancel first, so the server stops working on the query rather than
        just the client walking away.
        """
        started = time.perf_counter()
        deadline = time.monotonic() + timeout if timeout is not None and timeout > 0 else None
        # A pre-flight failure has no handle yet, so mint the id up front and
        # carry it into both the failure event and (on success) the handle —
        # the UI can then correlate every event to one run.
        query_id = uuid4().hex
        try:
            handle = await self._adapter.execute(sql, params)
        except Exception as exc:
            self._bus.publish(
                QueryFailed(
                    query_id=query_id,
                    error_code=exc.code if isinstance(exc, QueryError) else _UNEXPECTED,
                    message=str(exc),
                )
            )
            raise

        # Bind the query identity into the structured-logging contextvar so
        # every log line emitted while this run is in flight carries
        # ``query_id`` + ``adapter`` without each callsite needing to know.
        token = bind_context(query_id=handle.query_id, adapter=self._adapter.name)

        self._bus.publish(
            QueryStarted(
                query_id=handle.query_id,
                connection_id=handle.connection_id,
                sql_preview=sql[:120],
            )
        )

        # Capture the source iterator locally so the closure cannot recursively
        # iterate ``run.rows`` after we reassign it to the wrapper below.
        # If ``stream()`` fails here, ``_wrap``'s ``finally`` never runs, so we
        # must clear the logging context ourselves.
        try:
            source = await self._adapter.stream(handle, batch_size=batch_size)
        except BaseException:
            reset_context(token)
            raise
        run = QueryRun(handle=handle, rows=source)

        def _elapsed_ms() -> float:
            return (time.perf_counter() - started) * 1000.0

        async def _next_batch(iterator: AsyncIterator[ResultBatch]) -> ResultBatch | None:
            """Pull the next batch, bounded by the run's deadline."""
            if deadline is None:
                return await anext(iterator, None)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                return await anext(iterator, None)

        async def _wrap() -> AsyncIterator[ResultBatch]:
            next_progress_at = progress_every
            try:
                while True:
                    try:
                        batch = await _next_batch(source)
                    except TimeoutError as exc:
                        if deadline is None:
                            # No deadline of ours expired: this ``TimeoutError``
                            # came from the driver itself (asyncpg/asyncmy read
                            # timeouts, and ``asyncio.TimeoutError`` *is*
                            # ``TimeoutError``). Reporting it as our timeout
                            # formatted a ``None`` and raised TypeError, which
                            # then masked the real failure.
                            raise
                        # Ask the server to stop too — walking away client-side
                        # would leave the query burning resources over there.
                        with contextlib.suppress(Exception):
                            await self._adapter.cancel(handle)
                        handle.mark_done(QueryStatus.FAILED)
                        raise QueryTimeoutError(
                            f"query exceeded its {timeout:g}s timeout",
                            context={"query_id": handle.query_id, "timeout_seconds": timeout},
                        ) from exc
                    if batch is None:
                        break
                    run.rows_emitted += len(batch.rows)
                    if progress_every > 0 and run.rows_emitted >= next_progress_at:
                        # Skip past any thresholds a single large batch crossed
                        # so one batch never emits a burst of progress events.
                        next_progress_at = (run.rows_emitted // progress_every + 1) * progress_every
                        self._bus.publish(
                            QueryProgress(query_id=handle.query_id, rows_emitted=run.rows_emitted)
                        )
                    if run.cancel_event.is_set():
                        # Cooperative stop: the adapter's own cancellation may
                        # not interrupt a batch already in flight.
                        raise QueryCancelledError(
                            "query cancelled", context={"query_id": handle.query_id}
                        )
                    yield batch
                self._bus.publish(
                    QueryCompleted(
                        query_id=handle.query_id,
                        rows=run.rows_emitted,
                        duration_ms=_elapsed_ms(),
                    )
                )
            except GeneratorExit:
                # The consumer abandoned the stream (early ``break`` + aclose).
                # That is a cancellation, not a completion — reporting it as a
                # success would record a partial read as the full result.
                if handle.finished_at is None:
                    handle.mark_done(QueryStatus.CANCELLED)
                self._bus.publish(QueryCancelled(query_id=handle.query_id))
                raise
            except QueryCancelledError:
                self._bus.publish(QueryCancelled(query_id=handle.query_id))
                raise
            except asyncio.CancelledError:
                # CancelledError is a BaseException, so without this the run
                # would end with no terminal event and leave the UI stuck on
                # "running".
                self._bus.publish(QueryCancelled(query_id=handle.query_id))
                raise
            except Exception as exc:
                self._bus.publish(
                    QueryFailed(
                        query_id=handle.query_id,
                        error_code=exc.code if isinstance(exc, QueryError) else _UNEXPECTED,
                        message=str(exc),
                    )
                )
                raise
            finally:
                # Finalize the adapter's generator too. Closing ``_wrap`` alone
                # leaves ``source`` suspended at its own ``yield``, so the
                # driver cursor — and, on Postgres, the transaction wrapping the
                # server-side cursor — would survive until the garbage collector
                # got round to it, which is after the pool has already handed the
                # connection to the next query.
                # ``DatabaseAdapter.stream`` is typed as a bare AsyncIterator,
                # so probe for ``aclose`` the same way ``QueryRun.aclose`` does.
                source_aclose = getattr(source, "aclose", None)
                if source_aclose is not None:
                    try:
                        await source_aclose()
                    except Exception:
                        logger.warning(
                            "adapter_stream_close_failed", query_id=handle.query_id, exc_info=True
                        )
                reset_context(token)

        run.rows = _wrap()
        return run

    async def cancel(self, run: QueryRun) -> None:
        """Cooperative cancellation; safe to call multiple times."""
        run.cancel_event.set()
        await self._adapter.cancel(run.handle)
        logger.info("query_cancel_requested", query_id=run.handle.query_id)
