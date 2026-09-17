"""Query history persisted in a local SQLite store under ``~/.registro``."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from pydantic import BaseModel, ConfigDict, Field

from registro_core.logging import get_logger

logger = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id TEXT NOT NULL,
    connection_label TEXT NOT NULL,
    sql TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms REAL,
    rows INTEGER,
    error TEXT,
    started_at TEXT NOT NULL
);
-- Dropped, not created: both readers (``recent`` and ``_trim``) order by ``id``,
-- so this index was never used for a lookup and only cost a write per insert.
-- The DROP keeps databases created by earlier versions from paying for it.
DROP INDEX IF EXISTS idx_history_started;
"""


class HistoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int | None = None
    query_id: str
    connection_label: str
    sql: str
    status: str
    duration_ms: float | None = None
    rows: int | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class QueryHistory:
    """Append-only history with a bounded retention window."""

    def __init__(self, path: Path | None = None, *, max_entries: int = 1000) -> None:
        self._path = path or (Path.home() / ".registro" / "history.db")
        self._max = max_entries
        self._conn: aiosqlite.Connection | None = None
        self._inserts_since_trim = 0

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(Exception):
            self._path.parent.chmod(0o700)
        if not self._path.exists():
            with contextlib.suppress(Exception):
                self._path.touch()
        with contextlib.suppress(Exception):
            self._path.chmod(0o600)
        self._conn = await aiosqlite.connect(self._path)
        with contextlib.suppress(aiosqlite.Error):
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        await self._trim()
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            if self._inserts_since_trim > 0:
                with contextlib.suppress(Exception):
                    await self._trim()
                    await self._conn.commit()
            await self._conn.close()
            self._conn = None

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("QueryHistory not opened")
        return self._conn

    async def add(self, entry: HistoryEntry) -> int:
        conn = self._require()
        cur = await conn.execute(
            "INSERT INTO history "
            "(query_id, connection_label, sql, status, duration_ms, rows, error, started_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                entry.query_id,
                entry.connection_label,
                entry.sql,
                entry.status,
                entry.duration_ms,
                entry.rows,
                entry.error,
                entry.started_at.isoformat(),
            ),
        )
        rowid = cur.lastrowid or 0
        await cur.close()
        self._inserts_since_trim += 1
        if self._inserts_since_trim >= 50 or self._max <= 10:
            await self._trim()
            self._inserts_since_trim = 0
        await conn.commit()
        return rowid

    async def recent(self, limit: int = 50) -> list[HistoryEntry]:
        conn = self._require()
        # Ordered by ``id`` (insertion order) rather than by the ISO text in
        # ``started_at``, which sorts incorrectly once entries carry different
        # UTC offsets.
        async with conn.execute(
            "SELECT id, query_id, connection_label, sql, status, duration_ms,"
            " rows, error, started_at FROM history ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        entries: list[HistoryEntry] = []
        for r in rows:
            try:
                started_at = datetime.fromisoformat(r[8])
            except (ValueError, TypeError):
                # Corrupt or legacy timestamp: skip this row rather than abort
                # the whole fetch.
                logger.warning("history_row_skipped", history_id=r[0], started_at=r[8])
                continue
            entries.append(
                HistoryEntry(
                    id=r[0],
                    query_id=r[1],
                    connection_label=r[2],
                    sql=r[3],
                    status=r[4],
                    duration_ms=r[5],
                    rows=r[6],
                    error=r[7],
                    started_at=started_at,
                )
            )
        return entries

    async def _trim(self) -> None:
        conn = self._require()
        # Retention by rowid rather than by ``started_at``: ``id`` is
        # AUTOINCREMENT so it already orders by insertion, it is the primary
        # key (so this is an index scan, not a sort of the whole table every
        # 50 inserts), and it does not depend on timestamps whose text
        # representation sorts wrongly across UTC offsets.
        # The subquery yields NULL below the retention limit, and
        # ``id <= NULL`` matches nothing — so a small table is left alone.
        await conn.execute(
            "DELETE FROM history WHERE id <= ("
            "  SELECT id FROM history ORDER BY id DESC LIMIT 1 OFFSET ?"
            ")",
            (self._max,),
        )
