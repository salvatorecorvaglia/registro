from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from registro_engine.history import HistoryEntry, QueryHistory

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_history_round_trip(tmp_path: Path) -> None:
    h = QueryHistory(tmp_path / "h.db", max_entries=3)
    await h.open()
    try:
        for i in range(5):
            await h.add(
                HistoryEntry(
                    query_id=f"q{i}",
                    connection_label="sqlite:///:memory:",
                    sql=f"SELECT {i}",
                    status="succeeded",
                    duration_ms=1.0,
                    rows=1,
                )
            )
        recent = await h.recent(limit=10)
        # Bounded at max_entries=3
        assert len(recent) == 3
        # Most recent first
        assert recent[0].query_id == "q4"
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_recent_skips_rows_with_corrupt_timestamp(tmp_path: Path) -> None:
    h = QueryHistory(tmp_path / "h.db", max_entries=10)
    await h.open()
    try:
        await h.add(
            HistoryEntry(
                query_id="ok",
                connection_label="sqlite:///:memory:",
                sql="SELECT 1",
                status="succeeded",
            )
        )
        # Corrupt the stored timestamp directly.
        conn = h._require()
        await conn.execute("UPDATE history SET started_at='not-a-date' WHERE query_id='ok'")
        await conn.commit()
        # A bad row is skipped rather than aborting the whole fetch.
        assert await h.recent(limit=10) == []
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_history_trim_commits_on_open(tmp_path: Path) -> None:
    db_path = tmp_path / "trim_test.db"

    # 1. Open with large retention limit and write 5 entries
    h = QueryHistory(db_path, max_entries=10)
    await h.open()
    try:
        for i in range(5):
            await h.add(
                HistoryEntry(
                    query_id=f"q{i}",
                    connection_label="sqlite:///:memory:",
                    sql=f"SELECT {i}",
                    status="succeeded",
                )
            )
    finally:
        await h.close()

    # 2. Re-open with smaller retention limit (3) to trigger trim, then close immediately
    h2 = QueryHistory(db_path, max_entries=3)
    await h2.open()
    await h2.close()  # _inserts_since_trim is 0, so close() won't trim/commit again

    # 3. Verify trim was committed
    h3 = QueryHistory(db_path, max_entries=10)
    await h3.open()
    try:
        recent = await h3.recent(limit=10)
        assert len(recent) == 3
    finally:
        await h3.close()
