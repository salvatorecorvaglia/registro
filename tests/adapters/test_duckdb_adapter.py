from __future__ import annotations

import asyncio
from typing import Any

import pytest

duckdb = pytest.importorskip("duckdb")

from registro_adapters.duckdb_adapter import DuckDBAdapter  # noqa: E402
from registro_core.errors import QueryCancelledError  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_core.models.query import QueryStatus  # noqa: E402


@pytest.mark.asyncio
async def test_duckdb_select() -> None:
    a = DuckDBAdapter()
    await a.connect(Dsn.parse("duckdb:///:memory:"))
    try:
        h = await a.execute("SELECT 42 AS answer, 'hi' AS greeting")
        rows: list[tuple[Any, ...]] = []
        cols: list[str] = []
        async for batch in await a.stream(h):
            cols = [c.name for c in batch.columns] or cols
            rows.extend(batch.rows)
        assert cols == ["answer", "greeting"]
        rows = [r for r in rows if r]
        assert rows == [(42, "hi")]
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_duckdb_cancel_interrupts_running_query() -> None:
    a = DuckDBAdapter()
    await a.connect(Dsn.parse("duckdb:///:memory:"))
    try:
        # range() materializes lazily; pairing with a cross join produces a
        # CPU-bound stream that gives interrupt() something real to abort.
        h = await a.execute(
            "SELECT a.range, b.range FROM range(10_000_000) a CROSS JOIN range(100) b"
        )
        stream = await a.stream(h, batch_size=100)

        async def _drain() -> None:
            async for _ in stream:
                pass

        task = asyncio.create_task(_drain())
        await asyncio.sleep(0.05)
        await a.cancel(h)
        # ``QueryCancelledError`` specifically. The previous form,
        # ``pytest.raises((QueryCancelledError, Exception))``, is satisfied by
        # *any* exception — ``Exception`` subsumes the first member — so it
        # asserted only "something went wrong", not that cancellation was
        # translated into the adapter contract's error. The other three adapters
        # assert the concrete type in tests/adapters/test_cancellation_inflight.py.
        with pytest.raises(QueryCancelledError):
            await asyncio.wait_for(task, timeout=5.0)
        assert h.status is QueryStatus.CANCELLED
    finally:
        await a.close()


async def _drain(a: DuckDBAdapter, sql: str) -> list[tuple[Any, ...]]:
    h = await a.execute(sql)
    out: list[tuple[Any, ...]] = []
    async for batch in await a.stream(h):
        out.extend(r for r in batch.rows if r)
    return out


@pytest.mark.asyncio
async def test_duckdb_transaction_commit_persists() -> None:
    a = DuckDBAdapter()
    await a.connect(Dsn.parse("duckdb:///:memory:"))
    try:
        await _drain(a, "CREATE TABLE t (id INTEGER)")
        async with await a.begin():
            await _drain(a, "INSERT INTO t VALUES (1)")
        assert await _drain(a, "SELECT count(*) FROM t") == [(1,)]
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_duckdb_transaction_rollback_discards() -> None:
    a = DuckDBAdapter()
    await a.connect(Dsn.parse("duckdb:///:memory:"))
    try:
        await _drain(a, "CREATE TABLE t (id INTEGER)")
        await _drain(a, "INSERT INTO t VALUES (1)")

        async def _failing_txn() -> None:
            async with await a.begin():
                await _drain(a, "INSERT INTO t VALUES (2)")
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await _failing_txn()
        # The second insert must have been rolled back.
        assert await _drain(a, "SELECT count(*) FROM t") == [(1,)]
    finally:
        await a.close()


@pytest.mark.asyncio
async def test_duckdb_introspect_smoke() -> None:
    a = DuckDBAdapter()
    await a.connect(Dsn.parse("duckdb:///:memory:"))
    try:
        h = await a.execute("CREATE TABLE t (a INTEGER, b VARCHAR)")
        async for _ in await a.stream(h):
            pass
        snap = await a.introspect()
        assert any(obj.name == "t" for objs in snap.schemas.values() for obj in objs)
    finally:
        await a.close()
