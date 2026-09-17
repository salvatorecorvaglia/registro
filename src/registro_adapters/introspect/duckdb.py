"""DuckDB schema introspector."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from registro_adapters._introspect import SchemaIntrospector
from registro_adapters._typeinfer import infer_column_type
from registro_adapters.base import connection_id
from registro_core.models.schema import ForeignKey, Index, TableColumn

if TYPE_CHECKING:
    import duckdb

    from registro_core.models.connection import Dsn


def _index_columns(expressions: object) -> list[str]:
    """Normalize ``duckdb_indexes().expressions`` into a column list.

    DuckDB returns either a real list or its string rendering
    (``[a, b]``). Splitting the string on commas is wrong for an expression
    key containing one, so only the outer brackets are stripped when a list
    is not already given.
    """
    if isinstance(expressions, list | tuple):
        return [str(e).strip() for e in expressions if str(e).strip()]
    if expressions is None:
        return []
    text = str(expressions).strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [part.strip() for part in text.split(",") if part.strip()]


def _primary_key_columns(conn: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], frozenset[str]]:
    """Map ``(schema, table)`` to the names of its primary-key columns."""
    rows = conn.execute(
        "SELECT schema_name, table_name, constraint_column_names "
        "FROM duckdb_constraints() "
        "WHERE constraint_type = 'PRIMARY KEY'"
    ).fetchall()
    return {(schema, table): frozenset(cols) for schema, table, cols in rows}


class DuckDBIntrospector(SchemaIntrospector):
    """DuckDB-specific schema introspector.

    All synchronous DuckDB calls run via ``asyncio.to_thread`` under the
    adapter's connection lock to maintain DuckDB's single-threaded safety.
    """

    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        dsn: Dsn | None,
        lock: asyncio.Lock,
    ) -> None:
        self._conn = conn
        self._dsn = dsn
        self._lock = lock

    def connection_id(self) -> str:
        return connection_id(self._conn)

    def database_name(self) -> str | None:
        return self._dsn.database if self._dsn else None

    async def list_objects(self) -> list[tuple[str, str, str]]:
        conn = self._conn

        def _list() -> list[tuple[str, str, str]]:
            rows = conn.execute(
                "SELECT table_schema, table_name, table_type "
                "FROM information_schema.tables "
                "WHERE table_schema NOT IN ('pg_catalog','information_schema')"
            ).fetchall()
            result: list[tuple[str, str, str]] = []
            for schema_name, table_name, kind_raw in rows:
                kind = "view" if kind_raw == "VIEW" else "table"
                result.append((schema_name, table_name, kind))
            return result

        async with self._lock:
            return await asyncio.to_thread(_list)

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        conn = self._conn

        def _fetch() -> list[TableColumn]:
            cols = conn.execute(
                "SELECT "
                "    c.column_name, "
                "    c.data_type, "
                "    c.is_nullable, "
                "    c.column_default, "
                "    c.ordinal_position, "
                "    EXISTS ( "
                "        SELECT 1 "
                "        FROM information_schema.table_constraints tc "
                "        JOIN information_schema.key_column_usage kcu "
                "          ON tc.constraint_name = kcu.constraint_name "
                "         AND tc.table_schema = kcu.table_schema "
                "        WHERE tc.constraint_type = 'PRIMARY KEY' "
                "          AND tc.table_schema = c.table_schema "
                "          AND tc.table_name = c.table_name "
                "          AND kcu.column_name = c.column_name "
                "    ) AS is_primary "
                "FROM information_schema.columns c "
                "WHERE c.table_schema=? AND c.table_name=? "
                "ORDER BY c.ordinal_position",
                [schema, name],
            ).fetchall()
            return [
                TableColumn(
                    name=c[0],
                    type_native=c[1],
                    type=infer_column_type(c[1]),
                    nullable=c[2] == "YES",
                    default=c[3],
                    ordinal=c[4] or 0,
                    is_primary_key=bool(c[5]),
                )
                for c in cols
            ]

        async with self._lock:
            return await asyncio.to_thread(_fetch)

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        conn = self._conn

        def _fetch() -> list[ForeignKey]:
            rows = conn.execute(
                "SELECT constraint_name, constraint_column_names, "
                "referenced_table, referenced_column_names "
                "FROM duckdb_constraints() "
                "WHERE schema_name = ? AND table_name = ? AND constraint_type = 'FOREIGN KEY'",
                [schema, name],
            ).fetchall()
            return [
                ForeignKey(
                    name=row[0] or f"fk_{name}_{i}",
                    columns=list(row[1]),
                    referenced_table=row[2],
                    referenced_columns=list(row[3]),
                )
                for i, row in enumerate(rows)
            ]

        async with self._lock:
            return await asyncio.to_thread(_fetch)

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        conn = self._conn

        def _fetch() -> list[Index]:
            rows = conn.execute(
                "SELECT index_name, expressions, is_unique, is_primary "
                "FROM duckdb_indexes() "
                "WHERE schema_name = ? AND table_name = ?",
                [schema, name],
            ).fetchall()
            return [
                Index(
                    name=idx_name,
                    columns=_index_columns(exprs),
                    unique=bool(is_uniq),
                    primary=bool(is_pri),
                )
                for idx_name, exprs, is_uniq, is_pri in rows
            ]

        async with self._lock:
            return await asyncio.to_thread(_fetch)

    # ---- bulk variants -------------------------------------------------
    # One sweep per kind instead of three queries per table. Each is a single
    # round trip into the embedded engine, so a wide schema no longer costs
    # O(tables) thread hops on connect.

    async def fetch_all_columns(self) -> dict[tuple[str, str], list[TableColumn]]:
        conn = self._conn

        def _fetch() -> dict[tuple[str, str], list[TableColumn]]:
            rows = conn.execute(
                "SELECT c.table_schema, c.table_name, c.column_name, c.data_type, "
                "       c.is_nullable, c.column_default, c.ordinal_position "
                "FROM information_schema.columns c "
                "WHERE c.table_schema NOT IN ('pg_catalog','information_schema') "
                "ORDER BY c.table_schema, c.table_name, c.ordinal_position"
            ).fetchall()
            primary = _primary_key_columns(conn)
            res: dict[tuple[str, str], list[TableColumn]] = {}
            for schema, table, cname, ctype, nullable, default, ordinal in rows:
                key = (schema, table)
                res.setdefault(key, []).append(
                    TableColumn(
                        name=cname,
                        type_native=ctype,
                        type=infer_column_type(ctype),
                        nullable=nullable == "YES",
                        default=default,
                        ordinal=ordinal or 0,
                        is_primary_key=cname in primary.get(key, frozenset()),
                    )
                )
            return res

        async with self._lock:
            return await asyncio.to_thread(_fetch)

    async def fetch_all_foreign_keys(self) -> dict[tuple[str, str], list[ForeignKey]]:
        conn = self._conn

        def _fetch() -> dict[tuple[str, str], list[ForeignKey]]:
            rows = conn.execute(
                "SELECT schema_name, table_name, constraint_name, "
                "       constraint_column_names, referenced_table, referenced_column_names "
                "FROM duckdb_constraints() "
                "WHERE constraint_type = 'FOREIGN KEY'"
            ).fetchall()
            res: dict[tuple[str, str], list[ForeignKey]] = {}
            for schema, table, cname, cols, ref_table, ref_cols in rows:
                key = (schema, table)
                existing = res.setdefault(key, [])
                existing.append(
                    ForeignKey(
                        name=cname or f"fk_{table}_{len(existing)}",
                        columns=list(cols),
                        referenced_table=ref_table,
                        referenced_columns=list(ref_cols),
                    )
                )
            return res

        async with self._lock:
            return await asyncio.to_thread(_fetch)

    async def fetch_all_indexes(self) -> dict[tuple[str, str], list[Index]]:
        conn = self._conn

        def _fetch() -> dict[tuple[str, str], list[Index]]:
            rows = conn.execute(
                "SELECT schema_name, table_name, index_name, expressions, "
                "       is_unique, is_primary "
                "FROM duckdb_indexes()"
            ).fetchall()
            res: dict[tuple[str, str], list[Index]] = {}
            for schema, table, idx_name, exprs, is_uniq, is_pri in rows:
                res.setdefault((schema, table), []).append(
                    Index(
                        name=idx_name,
                        columns=_index_columns(exprs),
                        unique=bool(is_uniq),
                        primary=bool(is_pri),
                    )
                )
            return res

        async with self._lock:
            return await asyncio.to_thread(_fetch)
