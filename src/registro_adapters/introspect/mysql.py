"""MySQL schema introspector."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from registro_adapters._introspect import SchemaIntrospector
from registro_adapters._typeinfer import infer_column_type
from registro_adapters.base import connection_id
from registro_core.models.schema import ForeignKey, Index, TableColumn

if TYPE_CHECKING:
    from registro_adapters.mysql_adapter import MySQLAdapter

# Schemas MySQL owns; never surfaced in the catalog tree.
_SYSTEM_SCHEMAS = "('mysql','performance_schema','information_schema','sys')"


class MySQLIntrospector(SchemaIntrospector):
    def __init__(self, adapter: MySQLAdapter, conn: Any) -> None:
        self._adapter = adapter
        self._conn = conn

    def connection_id(self) -> str:
        return connection_id(self._conn)

    def database_name(self) -> str | None:
        return self._adapter._dsn.database if self._adapter._dsn else None

    async def _fetch(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        cur = self._conn.cursor()
        try:
            await cur.execute(sql, params)
            return list(await cur.fetchall())
        finally:
            await cur.close()

    async def list_objects(self) -> list[tuple[str, str, str]]:
        rows = await self._fetch(
            "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE"
            " FROM information_schema.tables"
            f" WHERE TABLE_SCHEMA NOT IN {_SYSTEM_SCHEMAS}"
        )
        return [(schema, name, "view" if raw == "VIEW" else "table") for schema, name, raw in rows]

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        rows = await self._fetch(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
            "ORDINAL_POSITION, COLUMN_KEY FROM information_schema.columns "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            (schema, name),
        )
        return [
            TableColumn(
                name=c[0],
                type_native=c[1],
                type=infer_column_type(c[1]),
                nullable=c[2] == "YES",
                default=c[3],
                ordinal=c[4] or 0,
                is_primary_key=(c[5] == "PRI"),
            )
            for c in rows
        ]

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        rows = await self._fetch(
            "SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME,"
            " REFERENCED_COLUMN_NAME, ORDINAL_POSITION FROM information_schema.key_column_usage"
            " WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s"
            " AND REFERENCED_TABLE_NAME IS NOT NULL"
            " ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION",
            (schema, name),
        )
        grouped = defaultdict(list)
        for cn, col, ref_t, ref_c, ord_pos in rows:
            grouped[cn].append((ord_pos, col, ref_t, ref_c))

        fks = []
        for cn, parts in sorted(grouped.items()):
            parts.sort(key=lambda x: x[0])
            ref_t = parts[0][2]
            cols = [p[1] for p in parts]
            ref_cols = [p[3] for p in parts]
            fks.append(
                ForeignKey(
                    name=cn,
                    columns=cols,
                    referenced_table=ref_t,
                    referenced_columns=ref_cols,
                )
            )
        return fks

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        rows = await self._fetch(
            "SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE"
            " FROM information_schema.statistics"
            " WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s"
            " ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            (schema, name),
        )
        by_idx: dict[str, dict[str, Any]] = {}
        for ix_name, col_name, non_unique in rows:
            entry = by_idx.setdefault(ix_name, {"cols": [], "unique": non_unique == 0})
            entry["cols"].append(col_name)
        return [
            Index(
                name=ix_name,
                columns=info["cols"],
                unique=info["unique"],
                primary=ix_name == "PRIMARY",
            )
            for ix_name, info in by_idx.items()
        ]

    # ---- bulk variants -------------------------------------------------
    # A full introspection used to issue three queries *per table*. On a
    # schema with a few hundred tables that is a thousand-odd round trips on
    # every connect. One sweep per kind answers the same questions.

    async def fetch_all_columns(self) -> dict[tuple[str, str], list[TableColumn]]:
        rows = await self._fetch(
            "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE,"
            " COLUMN_DEFAULT, ORDINAL_POSITION, COLUMN_KEY"
            " FROM information_schema.columns"
            f" WHERE TABLE_SCHEMA NOT IN {_SYSTEM_SCHEMAS}"
            " ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION"
        )
        res: dict[tuple[str, str], list[TableColumn]] = {}
        for schema, table, cname, ctype, nullable, default, ordinal, key in rows:
            res.setdefault((schema, table), []).append(
                TableColumn(
                    name=cname,
                    type_native=ctype,
                    type=infer_column_type(ctype),
                    nullable=nullable == "YES",
                    default=default,
                    ordinal=ordinal or 0,
                    is_primary_key=(key == "PRI"),
                )
            )
        return res

    async def fetch_all_foreign_keys(self) -> dict[tuple[str, str], list[ForeignKey]]:
        rows = await self._fetch(
            "SELECT TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME,"
            " REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME, ORDINAL_POSITION"
            " FROM information_schema.key_column_usage"
            f" WHERE TABLE_SCHEMA NOT IN {_SYSTEM_SCHEMAS}"
            " AND REFERENCED_TABLE_NAME IS NOT NULL"
            " ORDER BY TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION"
        )
        grouped: dict[tuple[str, str, str], list[tuple[Any, ...]]] = defaultdict(list)
        for schema, table, cname, col, ref_t, ref_c, ord_pos in rows:
            grouped[(schema, table, cname)].append((ord_pos, col, ref_t, ref_c))

        res: dict[tuple[str, str], list[ForeignKey]] = {}
        for (schema, table, cname), parts in sorted(grouped.items()):
            parts.sort(key=lambda x: x[0])
            res.setdefault((schema, table), []).append(
                ForeignKey(
                    name=cname,
                    columns=[p[1] for p in parts],
                    referenced_table=parts[0][2],
                    referenced_columns=[p[3] for p in parts],
                )
            )
        return res

    async def fetch_all_indexes(self) -> dict[tuple[str, str], list[Index]]:
        rows = await self._fetch(
            "SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, COLUMN_NAME, NON_UNIQUE"
            " FROM information_schema.statistics"
            f" WHERE TABLE_SCHEMA NOT IN {_SYSTEM_SCHEMAS}"
            " ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"
        )
        grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
        for schema, table, ix_name, col_name, non_unique in rows:
            entry = grouped.setdefault(
                (schema, table, ix_name), {"cols": [], "unique": non_unique == 0}
            )
            entry["cols"].append(col_name)

        res: dict[tuple[str, str], list[Index]] = {}
        for (schema, table, ix_name), info in grouped.items():
            res.setdefault((schema, table), []).append(
                Index(
                    name=ix_name,
                    columns=info["cols"],
                    unique=info["unique"],
                    primary=ix_name == "PRIMARY",
                )
            )
        return res
