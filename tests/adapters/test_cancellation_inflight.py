from __future__ import annotations

import asyncio

import pytest

from registro_adapters.sqlite_adapter import SQLiteAdapter
from registro_core.errors import QueryCancelledError
from registro_core.models.connection import Dsn
from registro_core.models.query import QueryHandle


@pytest.mark.asyncio
async def test_sqlite_cancel_inflight() -> None:
    a = SQLiteAdapter()
    await a.connect(Dsn.parse("sqlite:///:memory:"))
    try:
        # Generate a large recursive sequence to keep SQLite busy streaming
        h = await a.execute(
            "WITH RECURSIVE t(x) AS ("
            "SELECT 1 UNION ALL SELECT x+1 FROM t LIMIT 200000"
            ") SELECT * FROM t"
        )
        stream = await a.stream(h, batch_size=100)

        async def cancel_later() -> None:
            await asyncio.sleep(0.02)
            await a.cancel(h)

        task = asyncio.create_task(cancel_later())
        try:
            with pytest.raises(QueryCancelledError):
                async for _ in stream:
                    pass
        finally:
            await task
    finally:
        await a.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_cancel_inflight(postgres_dsn: str) -> None:
    pytest.importorskip("asyncpg")
    from registro_adapters.postgres_adapter import PostgresAdapter  # noqa: PLC0415

    a = PostgresAdapter()
    await a.connect(Dsn.parse(postgres_dsn))
    try:
        # pg_sleep blocks the connection on the server
        h = await a.execute("SELECT pg_sleep(3)")
        stream = await a.stream(h)

        async def cancel_later() -> None:
            await asyncio.sleep(0.1)
            await a.cancel(h)

        task = asyncio.create_task(cancel_later())
        try:
            with pytest.raises(QueryCancelledError):
                async for _ in stream:
                    pass
        finally:
            await task
    finally:
        await a.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mysql_cancel_inflight(mysql_dsn: str) -> None:
    pytest.importorskip("asyncmy")
    from registro_adapters.mysql_adapter import MySQLAdapter  # noqa: PLC0415

    a = MySQLAdapter()
    await a.connect(Dsn.parse(mysql_dsn))
    try:

        async def cancel_later() -> None:
            while a._active_query_id is None:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            dummy_handle = QueryHandle(
                query_id=a._active_query_id,
                connection_id="dummy",
                sql="",
            )
            await a.cancel(dummy_handle)

        task = asyncio.create_task(cancel_later())
        try:
            h = await a.execute("SELECT SLEEP(3)")
            stream = await a.stream(h)
            with pytest.raises(QueryCancelledError):
                async for _ in stream:
                    pass
        finally:
            await task
    finally:
        await a.close()
