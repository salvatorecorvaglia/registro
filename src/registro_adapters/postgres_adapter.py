"""PostgreSQL adapter using asyncpg directly (native async, server-side cursors)."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, ClassVar

import asyncpg

from registro_adapters._params import coerce_params
from registro_adapters._typeinfer import infer_column_type
from registro_adapters.base import AbstractAdapter, connection_id
from registro_adapters.introspect.postgres import PostgresIntrospector
from registro_core.errors import (
    AdapterConnectionError,
    AdapterError,
    QueryCancelledError,
    QueryError,
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

# Postgres itself caps a statement at 65535 bound parameters; anything past
# that is a caller mistake, and the value sizes an allocation.
_MAX_PLACEHOLDERS = 65535

# DSN parameters a Postgres URL may carry. asyncpg parses these out of the URL
# itself, which is exactly why they need gating: without an allowlist the query
# string is an open door to every libpq setting — ``options=-c ...`` can change
# server behaviour mid-connection. The other three adapters have applied this
# discipline since ``_params.py`` was introduced; Postgres was the gap.
_ALLOWED_PARAMS = frozenset(
    {
        "sslmode",
        "sslrootcert",
        "sslcert",
        "sslkey",
        "application_name",
        "connect_timeout",
        "target_session_attrs",
    }
)


class _PostgresTransaction:
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn
        self._tx: Any = None

    async def __aenter__(self) -> _PostgresTransaction:
        self._tx = self._conn.transaction()
        await self._tx.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            await self.commit()
        else:
            await self.rollback()

    async def commit(self) -> None:
        if self._tx is not None:
            await self._tx.commit()
            self._tx = None

    async def rollback(self) -> None:
        if self._tx is not None:
            await self._tx.rollback()
            self._tx = None


class PostgresAdapter(AbstractAdapter):
    name: ClassVar[str] = "postgres"
    schemes: ClassVar[tuple[str, ...]] = ("postgres", "postgresql")

    def __init__(self) -> None:
        super().__init__()
        self._conn: asyncpg.Connection | None = None
        self._backend_pid: int | None = None
        self._active_query_id: str | None = None
        self._statements: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def _do_connect(self, dsn: Dsn) -> None:
        # Validate before connecting. The parameters travel inside the URL that
        # asyncpg parses, so this is the only point at which an unknown one can
        # be refused by name rather than silently reaching libpq. The coerced
        # values are unused — asyncpg does its own parsing — but routing through
        # ``coerce_params`` keeps the rejection message identical across adapters.
        coerce_params(dsn.params, _ALLOWED_PARAMS, adapter="postgres")
        try:
            self._conn = await asyncpg.connect(dsn.render(mask_password=False))
            self._backend_pid = await self._conn.fetchval("SELECT pg_backend_pid()")
        except Exception as exc:
            raise AdapterConnectionError(
                f"postgres: connection failed: {exc}",
                context={"dsn": dsn.render(mask_password=True)},
            ) from exc

    async def _do_close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
        self._backend_pid = None
        self._active_query_id = None
        self._statements.clear()

    async def ping(self) -> bool:
        if self._conn is None:
            return False
        try:
            await self._conn.fetchval("SELECT 1")
            return True
        except asyncpg.PostgresError:
            return False

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> QueryHandle:
        conn = self._require_conn(self._conn)
        handle = QueryHandle(
            connection_id=connection_id(conn),
            sql=sql,
            params=dict(params) if params else None,
        )
        handle.mark_running()
        self._active_query_id = handle.query_id
        # Prepare here rather than in stream(). Preparing validates the SQL
        # server-side, so a syntax or permission error surfaces from execute()
        # — the same contract the other adapters offer. Running the statement
        # is still deferred, so a SELECT can stream from a server-side cursor.
        try:
            stmt = await conn.prepare(sql)
        except asyncpg.PostgresError as exc:
            handle.mark_done(QueryStatus.FAILED)
            self._active_query_id = None
            raise QueryError(str(exc), context={"sql": sql}) from exc
        self._statements[handle.query_id] = stmt
        handle.mark_streaming()
        return handle

    async def stream(
        self, handle: QueryHandle, *, batch_size: int = 1000
    ) -> AsyncIterator[ResultBatch]:
        conn = self._require_conn(self._conn)
        sql = handle.sql
        if not sql:
            raise AdapterError("postgres: stream() called with an empty handle")
        cancel = self._cancel_event(handle)
        stmt = self._statements.get(handle.query_id)
        if stmt is None:
            raise AdapterError("postgres: stream() called with unknown handle")

        # Convert dict parameters to positional list for asyncpg.
        # Postgres uses $1, $2... placeholders, so keys must be stringified positive integers.
        # We construct a list where args[i] corresponds to $(i+1).
        args: list[Any] = []
        if handle.params:
            try:
                keys = [int(k) for k in handle.params]
                if any(k <= 0 for k in keys):
                    raise ValueError("Placeholder indices must be positive integers >= 1")
                max_key = max(keys) if keys else 0
                if max_key > _MAX_PLACEHOLDERS:
                    # ``args`` is sized from this value, so an unbounded key
                    # would let a typo like {"999999999": 1} allocate a list of
                    # a billion entries.
                    raise ValueError(
                        f"Placeholder index {max_key} exceeds the maximum of {_MAX_PLACEHOLDERS}"
                    )
                args = [None] * max_key
                for k, v in handle.params.items():
                    args[int(k) - 1] = v
            except ValueError as exc:
                raise QueryError(
                    "postgres: invalid parameters. Parameters must be a dictionary "
                    f"with stringified integer keys from 1 to {_MAX_PLACEHOLDERS}, "
                    "corresponding to $1, $2, etc.",
                    context={"sql": sql, "params": handle.params, "error": str(exc)},
                ) from exc

        async def _gen() -> AsyncIterator[ResultBatch]:
            try:
                attrs = stmt.get_attributes()
                if not attrs:
                    # DML / DDL (no columns returned) — execute and finalize
                    try:
                        await stmt.fetch(*args)
                        status = stmt.get_statusmsg()
                    except asyncpg.PostgresError as exc:
                        self._wrap_driver_error(exc, handle, sql=sql)
                    handle.mark_done(QueryStatus.SUCCEEDED)
                    self._set_metadata(
                        handle,
                        QueryMetadata(server_message=status, column_names=[], column_types=[]),
                    )
                    yield ResultBatch(columns=[], rows=[], is_last=True)
                    return

                # DQL / RETURNING — stream using cursor
                columns = [
                    Column(
                        name=a.name,
                        type=infer_column_type(a.type.name),
                        type_native=a.type.name,
                    )
                    for a in attrs
                ]
                self._set_metadata(
                    handle,
                    QueryMetadata(
                        column_names=[c.name for c in columns],
                        column_types=[c.type_native or "" for c in columns],
                    ),
                )

                try:
                    async with conn.transaction():
                        cur = await stmt.cursor(*args)
                        while True:
                            if cancel.is_set():
                                handle.mark_done(QueryStatus.CANCELLED)
                                raise QueryCancelledError(
                                    "query cancelled", context={"query_id": handle.query_id}
                                )
                            records = await cur.fetch(batch_size)
                            if not records:
                                handle.mark_done(QueryStatus.SUCCEEDED)
                                yield ResultBatch(columns=columns, rows=[], is_last=True)
                                return
                            yield ResultBatch(
                                columns=columns,
                                rows=[tuple(r.values()) for r in records],
                                is_last=False,
                            )
                except asyncpg.PostgresError as exc:
                    self._wrap_driver_error(exc, handle, sql=sql)
            finally:
                self._forget(handle)

        return _gen()

    async def cancel(self, handle: QueryHandle) -> None:
        await super().cancel(handle)
        if self._active_query_id != handle.query_id:
            return
        # The live connection is busy running the user's query, so we can't
        # use it to issue pg_cancel_backend — open a short-lived side
        # connection instead and target the saved backend PID.
        if self._backend_pid is None or self._dsn is None:
            return
        dsn = self._dsn
        try:
            side = await asyncio.wait_for(
                asyncpg.connect(dsn.render(mask_password=False)),
                timeout=5.0,
            )
        except (asyncpg.PostgresError, OSError, TimeoutError) as exc:
            logger.warning(
                "postgres_cancel_side_connect_failed",
                query_id=handle.query_id,
                error=str(exc),
            )
            return
        try:
            with contextlib.suppress(asyncpg.PostgresError):
                await side.execute("SELECT pg_cancel_backend($1)", self._backend_pid)
        finally:
            with contextlib.suppress(Exception):
                await side.close()

    def _forget(self, handle: QueryHandle) -> None:
        super()._forget(handle)
        self._statements.pop(handle.query_id, None)
        if self._active_query_id == handle.query_id:
            self._active_query_id = None

    async def reset(self) -> None:
        await super().reset()
        self._active_query_id = None
        self._statements.clear()
        if self._conn is None:
            return
        # A stream abandoned mid-cursor leaves the connection inside the
        # transaction that wrapped it. Handing that to the next query would
        # run it in a foreign — possibly already-aborted — transaction.
        with contextlib.suppress(Exception):
            top_xact = getattr(self._conn, "_top_xact", None)
            if top_xact is not None:
                await top_xact.rollback()
            elif self._conn.is_in_transaction():
                await self._conn.execute("ROLLBACK")
        if hasattr(self._conn, "_top_xact"):
            self._conn._top_xact = None

    async def begin(self) -> Transaction:
        return _PostgresTransaction(self._require_conn(self._conn))

    async def introspect(self, *, lazy: bool = False) -> SchemaSnapshot:
        conn = self._require_conn(self._conn)
        async with self._lock:
            return await PostgresIntrospector(self, conn).build(lazy=lazy)

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        conn = self._require_conn(self._conn)
        async with self._lock:
            return await PostgresIntrospector(self, conn).fetch_columns(schema, name)

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        conn = self._require_conn(self._conn)
        async with self._lock:
            return await PostgresIntrospector(self, conn).fetch_foreign_keys(schema, name)

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        conn = self._require_conn(self._conn)
        async with self._lock:
            return await PostgresIntrospector(self, conn).fetch_indexes(schema, name)
