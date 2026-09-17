"""SQLite adapter using aiosqlite (native async)."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlencode

import aiosqlite

from registro_adapters._params import coerce_params
from registro_adapters._typeinfer import infer_column_type_from_value
from registro_adapters.base import AbstractAdapter, connection_id
from registro_adapters.introspect.sqlite import SQLiteIntrospector
from registro_core.errors import (
    AdapterConnectionError,
    AdapterError,
    QueryCancelledError,
)
from registro_core.logging import get_logger
from registro_core.models.query import QueryHandle, QueryMetadata, QueryStatus
from registro_core.models.result import Column, ColumnType, ResultBatch

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from registro_core.models.connection import Dsn
    from registro_core.models.schema import (
        ForeignKey,
        Index,
        SchemaSnapshot,
        TableColumn,
    )
    from registro_core.protocols.adapter import Transaction

logger = get_logger(__name__)

# Connect kwargs a DSN may set. ``uri`` is what makes shared-cache in-memory
# databases reachable by more than one connection.
_ALLOWED_PARAMS = frozenset(
    {"uri", "timeout", "detect_types", "cached_statements", "check_same_thread"}
)

# Parameters that belong to SQLite's own URI grammar rather than to
# ``sqlite3.connect``. They travel on the path (``file:db?mode=memory``), so a
# DSN like ``sqlite:///file:x?mode=memory&cache=shared&uri=true`` has to route
# them back onto the filename instead of passing them as connect kwargs.
_URI_PARAMS = frozenset({"mode", "cache", "immutable", "nolock", "psow", "vfs"})


def _split_uri_params(params: Mapping[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Split DSN params into (SQLite URI params, connect kwargs)."""
    uri_params = {k: v for k, v in params.items() if k in _URI_PARAMS}
    kwargs = {k: v for k, v in params.items() if k not in _URI_PARAMS}
    return uri_params, kwargs


def _typed_columns(columns: list[Column], sample: Sequence[Any]) -> list[Column]:
    """Re-type *columns* from a sample row, leaving names untouched."""
    typed = [
        col.model_copy(update={"type": infer_column_type_from_value(value)})
        for col, value in zip(columns, sample, strict=False)
    ]
    # A short row leaves the trailing columns as they were.
    return typed + columns[len(typed) :]


class _SQLiteTransaction:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _SQLiteTransaction:
        await self._conn.execute("BEGIN")
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()

    async def commit(self) -> None:
        await self._conn.execute("COMMIT")

    async def rollback(self) -> None:
        await self._conn.execute("ROLLBACK")


class SQLiteAdapter(AbstractAdapter):
    name: ClassVar[str] = "sqlite"
    schemes: ClassVar[tuple[str, ...]] = ("sqlite",)

    def __init__(self) -> None:
        super().__init__()
        self._conn: aiosqlite.Connection | None = None
        self._cursors: dict[str, aiosqlite.Cursor] = {}

    async def _do_connect(self, dsn: Dsn) -> None:
        path = dsn.database or ":memory:"
        uri_params, raw_kwargs = _split_uri_params(dsn.params)
        conn_kwargs: dict[str, Any] = {"isolation_level": None}
        conn_kwargs.update(coerce_params(raw_kwargs, _ALLOWED_PARAMS, adapter="sqlite"))
        if uri_params:
            separator = "&" if "?" in path else "?"
            path = path + separator + urlencode(uri_params)
            # SQLite only honours those parameters in URI mode.
            conn_kwargs["uri"] = True
        try:
            self._conn = await aiosqlite.connect(path, **conn_kwargs)
            # WAL mode is fine on file dbs, no-op on memory.
            if path != ":memory:":
                with contextlib.suppress(aiosqlite.Error):
                    await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
        except Exception as exc:
            raise AdapterConnectionError(
                f"sqlite: connection failed: {exc}",
                context={"path": path},
            ) from exc

    async def _do_close(self) -> None:
        for cur in list(self._cursors.values()):
            with contextlib.suppress(aiosqlite.Error):
                await cur.close()
        self._cursors.clear()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def ping(self) -> bool:
        if self._conn is None:
            return False
        try:
            await self._conn.execute("SELECT 1")
            return True
        except aiosqlite.Error:
            return False

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> QueryHandle:
        conn = self._require_conn(self._conn)
        handle = QueryHandle(
            connection_id=connection_id(conn),
            sql=sql,
            params=dict(params) if params else None,
        )
        handle.mark_running()
        try:
            cur = await conn.execute(sql, params or {})
        except aiosqlite.Error as exc:
            self._wrap_driver_error(exc, handle, sql=sql)
        self._cursors[handle.query_id] = cur
        handle.mark_streaming()
        return handle

    async def stream(
        self, handle: QueryHandle, *, batch_size: int = 1000
    ) -> AsyncIterator[ResultBatch]:
        cancel = self._cancel_event(handle)
        if cancel.is_set():
            return self._cancelled_stream(handle)

        cur = self._cursors.get(handle.query_id)
        if cur is None:
            raise AdapterError("sqlite: stream() called with unknown handle")

        async def _gen() -> AsyncIterator[ResultBatch]:
            # Everything below runs inside the ``try`` so that *every* exit path
            # reaches the ``finally``. A statement with no result columns
            # (DDL/DML) used to ``return`` above the ``try``, stranding its
            # cursor and cancel flag on the adapter for the life of the
            # connection — one leaked pair per INSERT/UPDATE/CREATE.
            try:
                description: list[Any] = list(cur.description or [])
                columns = [
                    Column(name=d[0], type=ColumnType.UNKNOWN, type_native=None)
                    for d in description
                ]
                self._set_metadata(
                    handle,
                    QueryMetadata(
                        column_names=[c.name for c in columns],
                        column_types=[],
                        affected_rows=cur.rowcount if cur.rowcount >= 0 else None,
                    ),
                )
                if not columns:
                    handle.mark_done(QueryStatus.SUCCEEDED)
                    yield ResultBatch(columns=[], rows=[], is_last=True)
                    return
                typed = False
                while True:
                    if cancel.is_set():
                        handle.mark_done(QueryStatus.CANCELLED)
                        raise QueryCancelledError(
                            "query cancelled", context={"query_id": handle.query_id}
                        )
                    try:
                        rows = await cur.fetchmany(batch_size)
                    except aiosqlite.Error as exc:
                        self._wrap_driver_error(exc, handle, mark_failed=False)
                    materialized = [tuple(r) for r in rows]
                    if not materialized:
                        handle.mark_done(QueryStatus.SUCCEEDED)
                        yield ResultBatch(columns=columns, rows=[], is_last=True)
                        return
                    if not typed:
                        # SQLite's cursor description carries names only, so the
                        # first row of data is the only type information there
                        # is. Types stay UNKNOWN if the result set is empty.
                        columns = _typed_columns(columns, materialized[0])
                        self._set_metadata(
                            handle,
                            QueryMetadata(
                                column_names=[c.name for c in columns],
                                column_types=[c.type.value for c in columns],
                                affected_rows=cur.rowcount if cur.rowcount >= 0 else None,
                            ),
                        )
                        typed = True
                    yield ResultBatch(columns=columns, rows=materialized, is_last=False)
            finally:
                await cur.close()
                self._cursors.pop(handle.query_id, None)
                self._forget(handle)

        return _gen()

    async def cancel(self, handle: QueryHandle) -> None:
        await super().cancel(handle)
        cur = self._cursors.pop(handle.query_id, None)
        if cur is not None:
            with contextlib.suppress(Exception):
                await cur.close()
        # sqlite3.Connection.interrupt() is thread-safe and causes any
        # in-flight statement on the connection to raise OperationalError.
        # We bypass aiosqlite's async wrapper because it queues onto the
        # same worker thread that's currently blocked on the long query.
        if self._conn is None:
            return
        inner: Any = getattr(self._conn, "_conn", None)
        if inner is None:
            return
        with contextlib.suppress(Exception):
            inner.interrupt()

    async def reset(self) -> None:
        await super().reset()
        for cur in list(self._cursors.values()):
            with contextlib.suppress(Exception):
                await cur.close()
        self._cursors.clear()
        if self._conn is not None and self._conn.in_transaction:
            # ``isolation_level=None`` means we only ever hold a transaction
            # that SQL explicitly opened, so anything still open here is a
            # leftover the next caller must not inherit.
            with contextlib.suppress(Exception):
                await self._conn.execute("ROLLBACK")

    async def begin(self) -> Transaction:
        return _SQLiteTransaction(self._require_conn(self._conn))

    async def introspect(self, *, lazy: bool = False) -> SchemaSnapshot:
        conn = self._require_conn(self._conn)
        return await SQLiteIntrospector(self, conn).build(lazy=lazy)

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        conn = self._require_conn(self._conn)
        return await SQLiteIntrospector(self, conn).fetch_columns(schema, name)

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        conn = self._require_conn(self._conn)
        return await SQLiteIntrospector(self, conn).fetch_foreign_keys(schema, name)

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        conn = self._require_conn(self._conn)
        return await SQLiteIntrospector(self, conn).fetch_indexes(schema, name)
