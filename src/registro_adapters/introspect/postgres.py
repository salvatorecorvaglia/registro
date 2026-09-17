"""PostgreSQL schema introspector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from registro_adapters._introspect import SchemaIntrospector
from registro_adapters._typeinfer import infer_column_type
from registro_adapters.base import connection_id
from registro_core.models.schema import ForeignKey, Index, TableColumn, View

if TYPE_CHECKING:
    from typing import Literal

    import asyncpg

    from registro_adapters.postgres_adapter import PostgresAdapter


# Columns come from pg_catalog rather than information_schema. The
# information_schema view answered "is this a primary key?" with a correlated
# EXISTS over table_constraints ⋈ key_column_usage, evaluated once per column —
# one of the slowest things you can ask the Postgres catalog for, and it ran
# across the whole database on every connect. The lateral join below reads the
# same fact straight off pg_index.
_COLUMNS_SELECT = """
    SELECT n.nspname                                  AS table_schema,
           c.relname                                  AS table_name,
           a.attname                                  AS column_name,
           format_type(a.atttypid, a.atttypmod)       AS data_type,
           NOT a.attnotnull                           AS is_nullable,
           pg_get_expr(d.adbin, d.adrelid)            AS column_default,
           a.attnum                                   AS ordinal_position,
           COALESCE(pk.is_primary, false)             AS is_primary
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    LEFT JOIN LATERAL (
        SELECT true AS is_primary
        FROM pg_index i
        WHERE i.indrelid = a.attrelid
          AND i.indisprimary
          AND a.attnum = ANY (i.indkey)
        LIMIT 1
    ) pk ON true
    WHERE a.attnum > 0
      AND NOT a.attisdropped
      AND c.relkind IN ('r', 'v', 'm', 'p')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
"""
_COLUMNS_ORDER = "ORDER BY n.nspname, c.relname, a.attnum"


def _to_column(row: Any) -> TableColumn:
    return TableColumn(
        name=row["column_name"],
        type_native=row["data_type"],
        type=infer_column_type(row["data_type"]),
        nullable=bool(row["is_nullable"]),
        default=row["column_default"],
        ordinal=row["ordinal_position"] or 0,
        is_primary_key=bool(row["is_primary"]),
    )


class PostgresIntrospector(SchemaIntrospector):
    def __init__(self, adapter: PostgresAdapter, conn: asyncpg.Connection) -> None:
        self._adapter = adapter
        self._conn = conn

    def connection_id(self) -> str:
        return connection_id(self._conn)

    def database_name(self) -> str | None:
        return self._adapter._dsn.database if self._adapter._dsn else None

    async def list_objects(self) -> list[tuple[str, str, str]]:
        rows = await self._conn.fetch(
            """
            SELECT n.nspname AS schema_name, c.relname AS name,
                   CASE c.relkind WHEN 'v' THEN 'view'
                                  WHEN 'm' THEN 'materialized_view'
                                  ELSE 'table' END AS kind
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r','v','m','p')
              AND n.nspname NOT IN ('pg_catalog','information_schema')
            ORDER BY n.nspname, c.relname
            """
        )
        return [(r["schema_name"], r["name"], r["kind"]) for r in rows]

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        rows = await self._conn.fetch(
            f"{_COLUMNS_SELECT} AND n.nspname = $1 AND c.relname = $2 {_COLUMNS_ORDER}",
            schema,
            name,
        )
        return [_to_column(r) for r in rows]

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        rows = await self._conn.fetch(
            """
            SELECT c.conname AS name,
                   rns.nspname AS ref_schema,
                   rcls.relname AS ref_table,
                   array_agg(att.attname ORDER BY u.ord) AS cols,
                   array_agg(ratt.attname ORDER BY u.ord) AS ref_cols
            FROM pg_constraint c
            JOIN pg_class cls ON cls.oid = c.conrelid
            JOIN pg_namespace ns ON ns.oid = cls.relnamespace
            JOIN pg_class rcls ON rcls.oid = c.confrelid
            JOIN pg_namespace rns ON rns.oid = rcls.relnamespace
            JOIN unnest(c.conkey, c.confkey) WITH ORDINALITY AS u(attnum, refattnum, ord)
                 ON true
            JOIN pg_attribute att
                 ON att.attrelid = c.conrelid AND att.attnum = u.attnum
            JOIN pg_attribute ratt
                 ON ratt.attrelid = c.confrelid AND ratt.attnum = u.refattnum
            WHERE c.contype = 'f' AND ns.nspname=$1 AND cls.relname=$2
            GROUP BY c.conname, rns.nspname, rcls.relname
            """,
            schema,
            name,
        )
        return [
            ForeignKey(
                name=fk["name"],
                columns=list(fk["cols"]),
                referenced_schema=fk["ref_schema"],
                referenced_table=fk["ref_table"],
                referenced_columns=list(fk["ref_cols"]),
            )
            for fk in rows
        ]

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        # ``pg_get_indexdef(oid, ord, true)`` renders each index key, including
        # expression keys. Joining pg_attribute on ``indkey`` instead — as this
        # used to — silently dropped them, because an expression key is stored
        # as attnum 0 and matches no column.
        rows = await self._conn.fetch(
            """
            SELECT i.relname AS name, ix.indisunique AS uniq, ix.indisprimary AS pk,
                   array_agg(
                       pg_get_indexdef(ix.indexrelid, k.ord::int, true) ORDER BY k.ord
                   ) AS cols
            FROM pg_index ix
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
            WHERE n.nspname=$1 AND t.relname=$2
            GROUP BY i.relname, ix.indisunique, ix.indisprimary
            """,
            schema,
            name,
        )
        return [
            Index(
                name=ix["name"],
                columns=list(ix["cols"]),
                unique=ix["uniq"],
                primary=ix["pk"],
            )
            for ix in rows
        ]

    async def fetch_all_columns(self) -> dict[tuple[str, str], list[TableColumn]]:
        rows = await self._conn.fetch(f"{_COLUMNS_SELECT} {_COLUMNS_ORDER}")
        res: dict[tuple[str, str], list[TableColumn]] = {}
        for c in rows:
            res.setdefault((c["table_schema"], c["table_name"]), []).append(_to_column(c))
        return res

    async def fetch_all_foreign_keys(self) -> dict[tuple[str, str], list[ForeignKey]]:
        rows = await self._conn.fetch(
            """
            SELECT ns.nspname AS schema, cls.relname AS table_name,
                   c.conname AS name,
                   rns.nspname AS ref_schema,
                   rcls.relname AS ref_table,
                   array_agg(att.attname ORDER BY u.ord) AS cols,
                   array_agg(ratt.attname ORDER BY u.ord) AS ref_cols
            FROM pg_constraint c
            JOIN pg_class cls ON cls.oid = c.conrelid
            JOIN pg_namespace ns ON ns.oid = cls.relnamespace
            JOIN pg_class rcls ON rcls.oid = c.confrelid
            JOIN pg_namespace rns ON rns.oid = rcls.relnamespace
            JOIN unnest(c.conkey, c.confkey) WITH ORDINALITY AS u(attnum, refattnum, ord)
                 ON true
            JOIN pg_attribute att
                 ON att.attrelid = c.conrelid AND att.attnum = u.attnum
            JOIN pg_attribute ratt
                 ON ratt.attrelid = c.confrelid AND ratt.attnum = u.refattnum
            WHERE c.contype = 'f' AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
            GROUP BY ns.nspname, cls.relname, c.conname, rns.nspname, rcls.relname
            """
        )
        res: dict[tuple[str, str], list[ForeignKey]] = {}
        for fk in rows:
            key = (fk["schema"], fk["table_name"])
            res.setdefault(key, []).append(
                ForeignKey(
                    name=fk["name"],
                    columns=list(fk["cols"]),
                    referenced_schema=fk["ref_schema"],
                    referenced_table=fk["ref_table"],
                    referenced_columns=list(fk["ref_cols"]),
                )
            )
        return res

    async def fetch_all_indexes(self) -> dict[tuple[str, str], list[Index]]:
        rows = await self._conn.fetch(
            """
            SELECT n.nspname AS schema, t.relname AS table_name,
                   i.relname AS name, ix.indisunique AS uniq, ix.indisprimary AS pk,
                   array_agg(
                       pg_get_indexdef(ix.indexrelid, k.ord::int, true) ORDER BY k.ord
                   ) AS cols
            FROM pg_index ix
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
            GROUP BY n.nspname, t.relname, i.relname, ix.indisunique, ix.indisprimary
            """
        )
        res: dict[tuple[str, str], list[Index]] = {}
        for ix in rows:
            key = (ix["schema"], ix["table_name"])
            res.setdefault(key, []).append(
                Index(
                    name=ix["name"],
                    columns=list(ix["cols"]),
                    unique=ix["uniq"],
                    primary=ix["pk"],
                )
            )
        return res

    def build_view(self, schema: str, name: str, kind: str, columns: list[TableColumn]) -> View:
        return View(
            name=name,
            schema_name=schema,
            columns=columns,
            kind=cast('Literal["view", "materialized_view"]', kind),
        )
