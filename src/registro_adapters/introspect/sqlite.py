"""SQLite schema introspector."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from registro_adapters._introspect import SchemaIntrospector
from registro_adapters._typeinfer import infer_column_type
from registro_adapters.base import connection_id
from registro_core.models.schema import ForeignKey, Index, TableColumn

if TYPE_CHECKING:
    import aiosqlite

    from registro_adapters.sqlite_adapter import SQLiteAdapter


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class SQLiteIntrospector(SchemaIntrospector):
    def __init__(self, adapter: SQLiteAdapter, conn: aiosqlite.Connection) -> None:
        self._adapter = adapter
        self._conn = conn

    def connection_id(self) -> str:
        return connection_id(self._conn)

    def database_name(self) -> str | None:
        return self._adapter._dsn.database if self._adapter._dsn else None

    async def list_objects(self) -> list[tuple[str, str, str]]:
        async with self._conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
        return [("main", name, kind) for name, kind in rows]

    async def fetch_columns(self, schema: str, name: str) -> list[TableColumn]:
        async with self._conn.execute(f"PRAGMA table_info({_quote_ident(name)})") as ccur:
            rows = await ccur.fetchall()
        return [
            TableColumn(
                name=cname,
                type_native=ctype or "",
                type=infer_column_type(ctype),
                nullable=not notnull,
                default=dflt,
                is_primary_key=bool(pk),
                ordinal=cid,
            )
            for cid, cname, ctype, notnull, dflt, pk in rows
        ]

    async def fetch_foreign_keys(self, schema: str, name: str) -> list[ForeignKey]:
        async with self._conn.execute(f"PRAGMA foreign_key_list({_quote_ident(name)})") as fcur:
            rows = await fcur.fetchall()
        grouped = defaultdict(list)
        for fk_id, seq, ref_table, fcol, tcol, on_update, on_delete, _match in rows:
            grouped[fk_id].append((seq, ref_table, fcol, tcol, on_update, on_delete))

        fks = []
        for fk_id, parts in sorted(grouped.items()):
            parts.sort(key=lambda x: x[0])
            ref_table = parts[0][1]
            on_update = parts[0][4]
            on_delete = parts[0][5]
            cols = [p[2] for p in parts]
            ref_cols = [p[3] for p in parts]
            fks.append(
                ForeignKey(
                    name=f"fk_{name}_{fk_id}",
                    columns=cols,
                    referenced_table=ref_table,
                    referenced_columns=ref_cols,
                    on_delete=on_delete,
                    on_update=on_update,
                )
            )
        return fks

    # ---- bulk variants -------------------------------------------------
    # SQLite has no catalog view of all columns, so the PRAGMAs still run per
    # table — but ``pragma_table_info`` is table-valued, so a single statement
    # joined against ``sqlite_master`` covers every table in one round trip
    # instead of one per table (plus one per index).

    async def fetch_all_columns(self) -> dict[tuple[str, str], list[TableColumn]]:
        async with self._conn.execute(
            "SELECT m.name AS table_name, p.name, p.type, p.'notnull', p.dflt_value, p.pk, p.cid "
            "FROM sqlite_master m "
            "JOIN pragma_table_info(m.name) p "
            "WHERE m.type IN ('table','view') AND m.name NOT LIKE 'sqlite_%' "
            "ORDER BY m.name, p.cid"
        ) as cur:
            rows = await cur.fetchall()
        res: dict[tuple[str, str], list[TableColumn]] = {}
        for table, cname, ctype, notnull, default, pk, cid in rows:
            res.setdefault(("main", table), []).append(
                TableColumn(
                    name=cname,
                    type_native=ctype or "",
                    type=infer_column_type(ctype),
                    nullable=not notnull,
                    default=default,
                    is_primary_key=bool(pk),
                    ordinal=cid,
                )
            )
        return res

    async def fetch_all_foreign_keys(self) -> dict[tuple[str, str], list[ForeignKey]]:
        async with self._conn.execute(
            "SELECT m.name AS table_name, f.id, f.seq, f.'table', f.'from', f.'to', "
            "       f.on_update, f.on_delete "
            "FROM sqlite_master m "
            "JOIN pragma_foreign_key_list(m.name) f "
            "WHERE m.type = 'table' AND m.name NOT LIKE 'sqlite_%' "
            "ORDER BY m.name, f.id, f.seq"
        ) as cur:
            rows = await cur.fetchall()

        grouped: dict[tuple[str, int], list[tuple[Any, ...]]] = defaultdict(list)
        for table, fk_id, seq, ref_table, fcol, tcol, on_update, on_delete in rows:
            grouped[(table, fk_id)].append((seq, ref_table, fcol, tcol, on_update, on_delete))

        res: dict[tuple[str, str], list[ForeignKey]] = {}
        for (table, fk_id), parts in sorted(grouped.items()):
            parts.sort(key=lambda x: x[0])
            res.setdefault(("main", table), []).append(
                ForeignKey(
                    name=f"fk_{table}_{fk_id}",
                    columns=[p[2] for p in parts],
                    referenced_table=parts[0][1],
                    referenced_columns=[p[3] for p in parts],
                    on_update=parts[0][4],
                    on_delete=parts[0][5],
                )
            )
        return res

    async def fetch_all_indexes(self) -> dict[tuple[str, str], list[Index]]:
        async with self._conn.execute(
            "SELECT m.name AS table_name, il.name AS index_name, il.'unique', ii.name, ii.seqno "
            "FROM sqlite_master m "
            "JOIN pragma_index_list(m.name) il "
            "JOIN pragma_index_info(il.name) ii "
            "WHERE m.type = 'table' AND m.name NOT LIKE 'sqlite_%' "
            "ORDER BY m.name, il.name, ii.seqno"
        ) as cur:
            rows = await cur.fetchall()

        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for table, index_name, unique, col_name, _seqno in rows:
            entry = grouped.setdefault((table, index_name), {"cols": [], "unique": bool(unique)})
            entry["cols"].append(col_name)

        res: dict[tuple[str, str], list[Index]] = {}
        for (table, index_name), info in grouped.items():
            res.setdefault(("main", table), []).append(
                Index(name=index_name, columns=info["cols"], unique=info["unique"])
            )
        return res

    async def fetch_indexes(self, schema: str, name: str) -> list[Index]:
        async with self._conn.execute(f"PRAGMA index_list({_quote_ident(name)})") as icur:
            idx_rows = await icur.fetchall()
        indexes: list[Index] = []
        for _seq, iname, unique, _origin, _partial in idx_rows:
            async with self._conn.execute(f"PRAGMA index_info({_quote_ident(iname)})") as iicur:
                icols = [r[2] for r in await iicur.fetchall()]
            indexes.append(Index(name=iname, columns=icols, unique=bool(unique)))
        return indexes
