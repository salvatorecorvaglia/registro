"""DuckDB adapter.

DuckDB's Python driver is sync; we wrap it with ``asyncio.to_thread`` and a
per-connection lock so the embedded DB is touched by one thread at a time.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, ClassVar

import duckdb

from registro_adapters._params import coerce_params
from registro_adapters._typeinfer import infer_column_type
from registro_adapters.base import AbstractAdapter, connection_id
from registro_adapters.introspect.duckdb import DuckDBIntrospector
from registro_core.errors import (
    AdapterConnectionError,
    AdapterError,
    QueryCancelledError,
)
from registro_core.logging import get_logger
from registro_core.models.query import QueryHandle, QueryMetadata, QueryStatus
from registro_core.models.result import Column, ResultBatch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from registro_core.models.connection import Dsn
    from registro_core.models.schema import (
        ForeignKey,
        Index,
        SchemaSnapshot,
        TableColumn,
    )
    from registro_core.protocols.adapter import Transaction

logger = get_logger(__name__)

_INTROSPECTION = "<introspection>"

# DuckDB settings a DSN may set. The full config surface includes switches that
# relax sandboxing (``allow_unsigned_extensions``, ``enable_external_access``),
# so a connection string gets an explicit, conservative subset.
_ALLOWED_PARAMS = frozenset(
    {"access_mode", "memory_limit", "threads", "max_memory", "temp_directory"}
)


class _DuckDBTransaction:
    def __init__(self, adapter: DuckDBAdapter) -> None:
        self._adapter = adapter

    async def __aenter__(self) -> _DuckDBTransaction:
        await self._adapter._exec_sync("BEGIN")
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()

    async def commit(self) -> None:
        await self._adapter._exec_sync("COMMIT")

    async def rollback(self) -> None:
        await self._adapter._exec_sync("ROLLBACK")


class DuckDBAdapter(AbstractAdapter):
    name: ClassVar[str] = "duckdb"
    schemes: ClassVar[tuple[str, ...]] = ("duckdb",)

    def __init__(self) -> None:
        super().__init__()
        self._conn: duckdb.DuckDBPyConnection | None = None
        self._lock = asyncio.Lock()
        # DuckDB's result set lives on the *connection*, not on a cursor: any
        # other statement executed before the current stream is drained silently
        # replaces the rows still being read. ``_streaming`` names the query that
        # owns the connection so a competing caller fails loudly instead.
        self._streaming: str | None = None

    def _claim_result_set(self, owner: str) -> None:
        if self._streaming is not None and self._streaming != owner:
            raise AdapterError(
                "duckdb: a result set is still being streamed on this connection; "
                "finish or cancel it before running another statement",
                context={"adapter": self.name, "streaming_query_id": self._streaming},
            )

    async def _do_connect(self, dsn: Dsn) -> None:
        path = dsn.database or ":memory:"
        config = coerce_params(dsn.params, _ALLOWED_PARAMS, adapter="duckdb")
        # ``access_mode=read_only`` is how DuckDB itself spells a read-only
        # connection; honour it instead of silently ignoring the request.
        read_only = str(config.pop("access_mode", "")).lower() == "read_only"
        try:
            # duckdb.connect is sync; wrap.
            if config:
                self._conn = await asyncio.to_thread(
                    duckdb.connect, path, read_only=read_only, config=config
                )
            else:
                self._conn = await asyncio.to_thread(duckdb.connect, path, read_only=read_only)
        except Exception as exc:
            raise AdapterConnectionError(
                f"duckdb: connection failed: {exc}",
                context={"path": path},
            ) from exc

    async def _do_close(self) -> None:
        self._streaming = None
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    async def ping(self) -> bool:
        if self._conn is None:
            return False
        try:
            await self._exec_sync("SELECT 1")
            return True
        except duckdb.Error:
            return False

    async def _exec_sync(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        conn = self._require_conn(self._conn)
        async with self._lock:

            def _run() -> Any:
                if params:
                    # Pass the mapping through unchanged so DuckDB binds by name
                    # ($name placeholders). Flattening to .values() would bind
                    # positionally and silently mismatch named parameters.
                    return conn.execute(sql, dict(params))
                return conn.execute(sql)

            return await asyncio.to_thread(_run)

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> QueryHandle:
        conn = self._require_conn(self._conn)
        handle = QueryHandle(
            connection_id=connection_id(conn),
            sql=sql,
            params=dict(params) if params else None,
        )
        handle.mark_running()
        self._claim_result_set(handle.query_id)
        try:
            await self._exec_sync(sql, params)
        except duckdb.Error as exc:
            self._wrap_driver_error(exc, handle, sql=sql)
        handle.mark_streaming()
        self._streaming = handle.query_id
        return handle

    async def stream(
        self, handle: QueryHandle, *, batch_size: int = 1000
    ) -> AsyncIterator[ResultBatch]:
        conn = self._require_conn(self._conn)
        cancel = self._cancel_event(handle)

        async def _gen() -> AsyncIterator[ResultBatch]:
            async with self._lock:
                description = await asyncio.to_thread(lambda: conn.description)
            columns = (
                [
                    Column(
                        name=d[0],
                        type=infer_column_type(str(d[1])),
                        type_native=str(d[1]),
                    )
                    for d in description
                ]
                if description
                else []
            )
            self._set_metadata(
                handle,
                QueryMetadata(
                    column_names=[c.name for c in columns],
                    column_types=[c.type_native or "" for c in columns],
                ),
            )
            try:
                if not columns:
                    handle.mark_done(QueryStatus.SUCCEEDED)
                    yield ResultBatch(columns=[], rows=[], is_last=True)
                    return
                while True:
                    if cancel.is_set():
                        handle.mark_done(QueryStatus.CANCELLED)
                        raise QueryCancelledError(
                            "query cancelled", context={"query_id": handle.query_id}
                        )
                    try:
                        async with self._lock:
                            rows = await asyncio.to_thread(conn.fetchmany, batch_size)
                    except duckdb.Error as exc:
                        self._wrap_driver_error(exc, handle, mark_failed=False)
                    if not rows:
                        handle.mark_done(QueryStatus.SUCCEEDED)
                        yield ResultBatch(columns=columns, rows=[], is_last=True)
                        return
                    yield ResultBatch(columns=columns, rows=[tuple(r) for r in rows], is_last=False)
            finally:
                if self._streaming == handle.query_id:
                    self._streaming = None
                self._forget(handle)

        return _gen()

    async def cancel(self, handle: QueryHandle) -> None:
        await super().cancel(handle)
        if self._streaming == handle.query_id:
            self._streaming = None
        if self._conn is None:
            return
        # interrupt() is thread-safe and aborts the in-progress query on the
        # other thread; we MUST NOT hold ``_lock`` here or we'd deadlock the
        # worker thread we're trying to interrupt.
        conn = self._conn
        try:
            await asyncio.to_thread(conn.interrupt)
        except Exception as exc:
            logger.warning("duckdb_interrupt_failed", query_id=handle.query_id, error=str(exc))

    async def reset(self) -> None:
        await super().reset()
        self._streaming = None
        if self._conn is None:
            return
        # Roll back anything an abandoned stream or a failed transaction block
        # left open. DuckDB errors when no transaction is active, which is the
        # common case — hence the suppression.
        with contextlib.suppress(Exception):
            await self._exec_sync("ROLLBACK")

    async def begin(self) -> Transaction:
        return _DuckDBTransaction(self)

    async def introspect(self, *, lazy: bool = False) -> SchemaSnapshot:
        conn = self._require_conn(self._conn)
        self._claim_result_set(_INTROSPECTION)
        introspector = DuckDBIntrospector(conn, self._dsn, self._lock)
        return await introspector.build(lazy=lazy)

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        conn = self._require_conn(self._conn)
        self._claim_result_set(_INTROSPECTION)
        introspector = DuckDBIntrospector(conn, self._dsn, self._lock)
        return await introspector.fetch_columns(schema, name)

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        conn = self._require_conn(self._conn)
        self._claim_result_set(_INTROSPECTION)
        introspector = DuckDBIntrospector(conn, self._dsn, self._lock)
        return await introspector.fetch_foreign_keys(schema, name)

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        conn = self._require_conn(self._conn)
        self._claim_result_set(_INTROSPECTION)
        introspector = DuckDBIntrospector(conn, self._dsn, self._lock)
        return await introspector.fetch_indexes(schema, name)
