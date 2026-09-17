"""MySQL adapter using asyncmy (native async)."""

from __future__ import annotations

import asyncio
import contextlib
import re
from typing import TYPE_CHECKING, Any, ClassVar

import asyncmy
from asyncmy import errors as asyncmy_errors
from asyncmy.constants import FIELD_TYPE
from asyncmy.cursors import SSCursor

from registro_adapters._params import coerce_params
from registro_adapters.base import AbstractAdapter, connection_id
from registro_adapters.introspect.mysql import MySQLIntrospector
from registro_core.errors import (
    AdapterConnectionError,
    AdapterError,
    QueryCancelledError,
    QueryError,
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

_PARAM_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# Connect kwargs a DSN may set. Deliberately narrow: the user/password/host/
# port/database come from the DSN proper, and everything else a driver accepts
# is not something a connection string should be able to reach into.
_ALLOWED_PARAMS = frozenset(
    {
        "charset",
        "connect_timeout",
        "ssl",
        "ssl_ca",
        "ssl_cert",
        "ssl_key",
        "ssl_disabled",
        "read_default_group",
        "program_name",
        "use_unicode",
        "echo",
    }
)


# MySQL's wire protocol reports a numeric field type per column. Mapping it is
# the only way to type a result set: ``information_schema`` describes stored
# tables, not the shape of an arbitrary SELECT.
#
# Mind the aliases in the driver's constants module: ``FIELD_TYPE.CHAR`` *is*
# ``FIELD_TYPE.TINY`` (both 1) and ``FIELD_TYPE.INTERVAL`` *is* ``FIELD_TYPE.YEAR``
# (both 13). Keying anything off an alias silently overwrites the canonical
# entry — which is how code 1 came to say STRING, mistyping every TINYINT and,
# since MySQL stores BOOLEAN as TINYINT(1), every boolean column with it. Only
# canonical names appear below.
_FIELD_TYPE_TO_COLUMN_TYPE: dict[int, ColumnType] = {
    FIELD_TYPE.DECIMAL: ColumnType.DECIMAL,
    FIELD_TYPE.NEWDECIMAL: ColumnType.DECIMAL,
    FIELD_TYPE.TINY: ColumnType.INTEGER,
    FIELD_TYPE.SHORT: ColumnType.INTEGER,
    FIELD_TYPE.LONG: ColumnType.INTEGER,
    FIELD_TYPE.LONGLONG: ColumnType.INTEGER,
    FIELD_TYPE.INT24: ColumnType.INTEGER,
    FIELD_TYPE.YEAR: ColumnType.INTEGER,
    FIELD_TYPE.FLOAT: ColumnType.FLOAT,
    FIELD_TYPE.DOUBLE: ColumnType.FLOAT,
    FIELD_TYPE.BIT: ColumnType.BOOL,
    FIELD_TYPE.DATE: ColumnType.DATE,
    FIELD_TYPE.NEWDATE: ColumnType.DATE,
    FIELD_TYPE.TIME: ColumnType.TIME,
    FIELD_TYPE.DATETIME: ColumnType.DATETIME,
    FIELD_TYPE.TIMESTAMP: ColumnType.DATETIME,
    FIELD_TYPE.JSON: ColumnType.JSON,
    FIELD_TYPE.BLOB: ColumnType.BINARY,
    FIELD_TYPE.TINY_BLOB: ColumnType.BINARY,
    FIELD_TYPE.MEDIUM_BLOB: ColumnType.BINARY,
    FIELD_TYPE.LONG_BLOB: ColumnType.BINARY,
    FIELD_TYPE.VARCHAR: ColumnType.STRING,
    FIELD_TYPE.VAR_STRING: ColumnType.STRING,
    FIELD_TYPE.STRING: ColumnType.STRING,
    FIELD_TYPE.ENUM: ColumnType.STRING,
    FIELD_TYPE.SET: ColumnType.STRING,
    FIELD_TYPE.NULL: ColumnType.NULL,
}

# Human-readable native names for the same codes, so the UI can show something
# more useful than a number. Built from an explicit list of canonical names
# rather than ``vars(FIELD_TYPE)``, whose iteration order would otherwise decide
# which of two aliased names wins.
_CANONICAL_FIELD_TYPE_NAMES: tuple[str, ...] = (
    "DECIMAL",
    "TINY",
    "SHORT",
    "LONG",
    "FLOAT",
    "DOUBLE",
    "NULL",
    "TIMESTAMP",
    "LONGLONG",
    "INT24",
    "DATE",
    "TIME",
    "DATETIME",
    "YEAR",
    "NEWDATE",
    "VARCHAR",
    "BIT",
    "JSON",
    "NEWDECIMAL",
    "ENUM",
    "SET",
    "TINY_BLOB",
    "MEDIUM_BLOB",
    "LONG_BLOB",
    "BLOB",
    "VAR_STRING",
    "STRING",
    "GEOMETRY",
)


def _canonical_field_type_names() -> dict[int, str]:
    """Map each field-type code to its canonical driver name."""
    names: dict[int, str] = {}
    for name in _CANONICAL_FIELD_TYPE_NAMES:
        code = getattr(FIELD_TYPE, name, None)
        if isinstance(code, int):
            names.setdefault(code, name)
    return names


_FIELD_TYPE_NAMES: dict[int, str] = _canonical_field_type_names()


def _describe_columns(description: Sequence[Any]) -> list[Column]:
    """Build typed :class:`Column`s from a DB-API cursor description."""
    columns: list[Column] = []
    for d in description:
        code = d[1] if len(d) > 1 else None
        native = _FIELD_TYPE_NAMES.get(code) if isinstance(code, int) else None
        col_type = (
            _FIELD_TYPE_TO_COLUMN_TYPE.get(code, ColumnType.UNKNOWN)
            if isinstance(code, int)
            else ColumnType.UNKNOWN
        )
        columns.append(Column(name=d[0], type=col_type, type_native=native))
    return columns


def _connect_kwargs(dsn: Dsn) -> dict[str, Any]:
    """Build asyncmy connect kwargs from a DSN, allowlisting its parameters."""
    kwargs: dict[str, Any] = {
        "user": dsn.username or "root",
        "password": dsn.password.get_secret_value() if dsn.password else "",
        "host": dsn.host or "localhost",
        "port": dsn.port or 3306,
        "database": dsn.database,
        "autocommit": True,
    }
    kwargs.update(coerce_params(dsn.params, _ALLOWED_PARAMS, adapter="mysql"))
    return kwargs


class _MySQLTransaction:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> _MySQLTransaction:
        await self._conn.begin()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()

    async def commit(self) -> None:
        await self._conn.commit()

    async def rollback(self) -> None:
        await self._conn.rollback()


class MySQLAdapter(AbstractAdapter):
    name: ClassVar[str] = "mysql"
    schemes: ClassVar[tuple[str, ...]] = ("mysql",)

    def __init__(self) -> None:
        super().__init__()
        self._conn: Any = None
        self._cursors: dict[str, Any] = {}
        self._thread_id: int | None = None
        self._active_query_id: str | None = None
        self._lock = asyncio.Lock()

    async def _do_connect(self, dsn: Dsn) -> None:
        conn_kwargs = _connect_kwargs(dsn)
        try:
            self._conn = await asyncmy.connect(**conn_kwargs)
            cur = self._conn.cursor()
            try:
                await cur.execute("SELECT CONNECTION_ID()")
                row = await cur.fetchone()
                self._thread_id = int(row[0]) if row else None
            finally:
                await cur.close()
        except Exception as exc:
            raise AdapterConnectionError(
                f"mysql: connection failed: {exc}",
                context={"dsn": dsn.render(mask_password=True)},
            ) from exc

    async def _do_close(self) -> None:
        for c in list(self._cursors.values()):
            with contextlib.suppress(Exception):
                await c.close()
        self._cursors.clear()
        if self._conn is not None:
            await self._conn.ensure_closed()
            self._conn = None
        self._thread_id = None
        self._active_query_id = None

    async def cancel(self, handle: QueryHandle) -> None:
        await super().cancel(handle)
        if self._active_query_id != handle.query_id:
            return
        cur = self._cursors.pop(handle.query_id, None)
        # Live connection is busy; open side connection & issue KILL QUERY first.
        # This unblocks the statement so we can safely close the cursor later.
        if self._thread_id is not None and self._dsn is not None:
            dsn = self._dsn
            try:
                side_kwargs = _connect_kwargs(dsn)
                side = await asyncio.wait_for(
                    asyncmy.connect(**side_kwargs),
                    timeout=5.0,
                )
                try:
                    scur = side.cursor()
                    try:
                        with contextlib.suppress(Exception):
                            await scur.execute(f"KILL QUERY {int(self._thread_id)}")
                    finally:
                        with contextlib.suppress(Exception):
                            await scur.close()
                finally:
                    with contextlib.suppress(Exception):
                        await side.ensure_closed()
            except Exception as exc:
                logger.warning(
                    "mysql_cancel_side_connect_failed",
                    query_id=handle.query_id,
                    error=str(exc),
                )

        # Now we can safely close the popped cursor
        if cur is not None:
            with contextlib.suppress(Exception):
                await cur.close()

    def _forget(self, handle: QueryHandle) -> None:
        super()._forget(handle)
        if self._active_query_id == handle.query_id:
            self._active_query_id = None

    async def reset(self) -> None:
        await super().reset()
        for cur in list(self._cursors.values()):
            with contextlib.suppress(Exception):
                await cur.close()
        self._cursors.clear()
        self._active_query_id = None
        if self._conn is not None:
            # Undo any transaction an abandoned stream or a failed BEGIN left
            # open; the connection is about to serve a different query.
            with contextlib.suppress(Exception):
                await self._conn.rollback()

    async def ping(self) -> bool:
        if self._conn is None:
            return False
        cur = self._conn.cursor()
        try:
            await cur.execute("SELECT 1")
            return True
        except asyncmy_errors.Error:
            return False
        finally:
            # ``close`` in a ``finally``: a failed ``SELECT 1`` used to leave the
            # cursor open, which is the same leak the 1.3.0 notes record fixing
            # in ``stream()``.
            with contextlib.suppress(Exception):
                await cur.close()

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> QueryHandle:
        conn = self._require_conn(self._conn)
        if params:
            for k in params:
                if not isinstance(k, str) or not _PARAM_NAME_RE.match(k):
                    raise QueryError(
                        f"mysql: invalid parameter name {k!r}. "
                        "Parameter names must be alphanumeric and start "
                        "with a letter or underscore.",
                        context={"sql": sql, "param_name": str(k)},
                    )
        handle = QueryHandle(
            connection_id=connection_id(conn),
            sql=sql,
            params=dict(params) if params else None,
        )
        handle.mark_running()
        self._active_query_id = handle.query_id
        # ``SSCursor``, not the default ``Cursor``. asyncmy's default cursor is
        # *buffered*: it reads the entire result set into Python during
        # ``execute()``, so ``fetchmany`` below was slicing a list that was
        # already fully in memory. That defeated the whole streaming path —
        # ``batch_size`` and the UI's display cap bounded nothing, peak memory
        # was the size of the result set, the first row could not reach the
        # screen until the last one had arrived, and cancelling mid-stream had
        # nothing left to cancel. The other three adapters all stream.
        cur = conn.cursor(cursor=SSCursor)
        try:
            # asyncmy uses the ``pyformat`` paramstyle: pass the mapping
            # directly so ``%(name)s`` placeholders bind by name.
            await cur.execute(sql, dict(params) if params else None)
        except asyncmy_errors.Error as exc:
            await cur.close()
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
            raise AdapterError("mysql: stream() called with unknown handle")

        async def _gen() -> AsyncIterator[ResultBatch]:
            # Everything below runs inside the ``try`` so that *every* exit path
            # reaches the ``finally``. A statement with no result columns
            # (DDL/DML) used to ``return`` above the ``try``, stranding its
            # cursor and cancel flag on the adapter for the life of the
            # connection — one leaked pair per INSERT/UPDATE/CREATE.
            try:
                columns = _describe_columns(cur.description or [])
                # An unbuffered cursor does not know a SELECT's row count until
                # the result set has been read, so ``rowcount`` is negative here
                # and reports as ``None``. It stays meaningful for DML, where the
                # server sends affected-rows in the OK packet before any fetch.
                self._set_metadata(
                    handle,
                    QueryMetadata(
                        column_names=[c.name for c in columns],
                        column_types=[c.type_native or "" for c in columns],
                        affected_rows=cur.rowcount if cur.rowcount >= 0 else None,
                    ),
                )
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
                        rows = await cur.fetchmany(batch_size)
                    except asyncmy_errors.Error as exc:
                        self._wrap_driver_error(exc, handle, mark_failed=False)
                    if not rows:
                        handle.mark_done(QueryStatus.SUCCEEDED)
                        yield ResultBatch(columns=columns, rows=[], is_last=True)
                        return
                    yield ResultBatch(columns=columns, rows=[tuple(r) for r in rows], is_last=False)
            finally:
                # Closing an unbuffered cursor drains whatever is still on the
                # wire, which is what leaves the connection reusable when the
                # consumer stops early (the UI's display cap, the CLI's
                # ``--limit``). Suppressed because a killed or broken query has
                # nothing left to drain and raises here.
                with contextlib.suppress(Exception):
                    await cur.close()
                self._cursors.pop(handle.query_id, None)
                self._forget(handle)

        return _gen()

    async def begin(self) -> Transaction:
        return _MySQLTransaction(self._require_conn(self._conn))

    async def introspect(self, *, lazy: bool = False) -> SchemaSnapshot:
        conn = self._require_conn(self._conn)
        async with self._lock:
            return await MySQLIntrospector(self, conn).build(lazy=lazy)

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        conn = self._require_conn(self._conn)
        async with self._lock:
            return await MySQLIntrospector(self, conn).fetch_columns(schema, name)

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        conn = self._require_conn(self._conn)
        async with self._lock:
            return await MySQLIntrospector(self, conn).fetch_foreign_keys(schema, name)

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        conn = self._require_conn(self._conn)
        async with self._lock:
            return await MySQLIntrospector(self, conn).fetch_indexes(schema, name)
