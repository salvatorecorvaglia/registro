from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("asyncpg")

import asyncpg

from registro_adapters.postgres_adapter import PostgresAdapter
from registro_core.errors import AdapterConnectionError, AdapterError, QueryError
from registro_core.models.connection import Dsn
from registro_core.models.query import QueryHandle, QueryStatus


def _adapter_with_statement(stmt: MagicMock) -> PostgresAdapter:
    adapter = PostgresAdapter()
    adapter._conn = MagicMock()
    adapter._conn.prepare = AsyncMock(return_value=stmt)
    adapter._conn.transaction.return_value = _fake_transaction()
    adapter._conn.is_in_transaction.return_value = False
    return adapter


def _fake_transaction() -> MagicMock:
    tx = MagicMock()
    tx.start = AsyncMock()
    tx.commit = AsyncMock()
    tx.rollback = AsyncMock()
    return tx


def _returning_statement(batches: list[list[dict[str, int]]]) -> MagicMock:
    """A statement with one ``id`` column whose cursor yields *batches*."""
    stmt = MagicMock()
    attr = MagicMock()
    attr.name = "id"
    attr.type.name = "integer"
    stmt.get_attributes.return_value = [attr]
    cursor = MagicMock()
    cursor.fetch = AsyncMock(side_effect=[*batches, []])
    stmt.cursor = AsyncMock(return_value=cursor)
    return stmt


@pytest.mark.asyncio
async def test_dml_statement_is_executed_and_reports_its_status() -> None:
    stmt = MagicMock()
    stmt.get_attributes.return_value = []
    stmt.fetch = AsyncMock(return_value=[])
    stmt.get_statusmsg = MagicMock(return_value="UPDATE 1")
    adapter = _adapter_with_statement(stmt)

    handle = await adapter.execute("UPDATE t SET name = $1 WHERE id = $2", {"1": "Alice", "2": 42})
    batches = [batch async for batch in await adapter.stream(handle)]

    assert len(batches) == 1
    assert batches[0].columns == []
    assert batches[0].rows == []
    stmt.fetch.assert_called_once_with("Alice", 42)
    meta = await adapter.metadata(handle)
    assert meta.server_message == "UPDATE 1"
    assert meta.affected_rows == 1


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("UPDATE 5", 5),
        ("INSERT 0 3", 3),
        ("DELETE 0", 0),
        ("SELECT 12", 12),
        ("CREATE TABLE", None),
        ("", None),
        (None, None),
    ],
)
def test_affected_rows_are_read_from_the_command_tag(tag: str | None, expected: int | None) -> None:
    from registro_adapters.postgres_adapter import _affected_rows  # noqa: PLC0415

    assert _affected_rows(tag) == expected


@pytest.mark.asyncio
async def test_select_streams_through_a_server_side_cursor() -> None:
    stmt = MagicMock()
    attr = MagicMock()
    attr.name = "id"
    attr.type.name = "integer"
    stmt.get_attributes.return_value = [attr]
    cursor = MagicMock()
    cursor.fetch = AsyncMock(side_effect=[[{"id": 42}], []])
    stmt.cursor = AsyncMock(return_value=cursor)
    adapter = _adapter_with_statement(stmt)

    handle = await adapter.execute("-- comment\nSELECT id FROM t WHERE name = $1", {"1": "Bob"})
    batches = [batch async for batch in await adapter.stream(handle)]

    assert len(batches) == 2
    assert batches[0].columns[0].name == "id"
    assert batches[0].rows == [(42,)]
    stmt.cursor.assert_called_once_with("Bob")


@pytest.mark.asyncio
async def test_execute_surfaces_a_bad_statement_like_every_other_adapter() -> None:
    """Preparing happens in ``execute()``, so SQL errors surface there."""
    adapter = PostgresAdapter()
    adapter._conn = MagicMock()
    adapter._conn.prepare = AsyncMock(side_effect=asyncpg.PostgresSyntaxError("boom"))

    with pytest.raises(QueryError):
        await adapter.execute("SELCT 1")


@pytest.mark.asyncio
async def test_stream_rejects_a_handle_execute_never_saw() -> None:
    adapter = PostgresAdapter()
    adapter._conn = MagicMock()
    stray = QueryHandle(connection_id="test", sql="SELECT 1")

    with pytest.raises(AdapterError, match="unknown handle"):
        await adapter.stream(stray)


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"name": "Alice"}, {"-1": "Alice"}, {"0": "Alice"}])
async def test_invalid_placeholder_keys_are_rejected(params: dict[str, object]) -> None:
    stmt = MagicMock()
    stmt.get_attributes.return_value = []
    adapter = _adapter_with_statement(stmt)

    handle = await adapter.execute("SELECT * FROM t WHERE name = $1", params)
    with pytest.raises(QueryError, match="invalid parameters"):
        await adapter.stream(handle)


@pytest.mark.asyncio
async def test_absurd_placeholder_index_is_refused_not_allocated() -> None:
    """``args`` is sized from the key, so an unbounded index is a memory bomb."""
    stmt = MagicMock()
    stmt.get_attributes.return_value = []
    adapter = _adapter_with_statement(stmt)

    handle = await adapter.execute("SELECT $1", {"999999999": "x"})
    with pytest.raises(QueryError, match="invalid parameters"):
        await adapter.stream(handle)


@pytest.mark.asyncio
async def test_parameter_gaps_are_filled_with_none() -> None:
    stmt = MagicMock()
    stmt.get_attributes.return_value = []
    stmt.fetch = AsyncMock(return_value=[])
    stmt.get_statusmsg = MagicMock(return_value="SELECT 1")
    adapter = _adapter_with_statement(stmt)

    handle = await adapter.execute("SELECT * FROM t WHERE name = $2", {"2": "Alice"})
    async for _ in await adapter.stream(handle):
        pass

    stmt.fetch.assert_called_once_with(None, "Alice")


@pytest.mark.asyncio
async def test_failed_execute_does_not_strand_the_active_query() -> None:
    adapter = PostgresAdapter()
    adapter._conn = MagicMock()
    adapter._conn.prepare = AsyncMock(side_effect=asyncpg.PostgresSyntaxError("boom"))

    with pytest.raises(QueryError):
        await adapter.execute("SELCT 1")
    assert adapter._active_query_id is None
    assert adapter._statements == {}


@pytest.mark.asyncio
async def test_streaming_a_result_forgets_its_prepared_statement() -> None:
    stmt = MagicMock()
    stmt.get_attributes.return_value = []
    stmt.fetch = AsyncMock(return_value=[])
    stmt.get_statusmsg = MagicMock(return_value="SELECT 0")
    adapter = _adapter_with_statement(stmt)

    handle = await adapter.execute("SELECT 1")
    assert adapter._statements
    async for _ in await adapter.stream(handle):
        pass
    assert adapter._statements == {}, "prepared statements accumulate per query"
    assert handle.status is QueryStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_unknown_dsn_parameters_are_refused_before_connecting() -> None:
    """Postgres must gate DSN parameters like every other adapter.

    The query string rides into asyncpg's own URL parser, so without an
    allowlist a DSN could reach any libpq setting — ``options=-c ...`` changes
    server behaviour. The refusal happens before any network access.
    """
    adapter = PostgresAdapter()
    dsn = Dsn.parse("postgres://u:p@localhost:5432/db?options=-c%20statement_timeout%3D1")

    with pytest.raises(AdapterConnectionError, match="unsupported DSN parameter"):
        await adapter.connect(dsn)

    assert adapter._conn is None
    assert adapter.connected is False


@pytest.mark.asyncio
async def test_known_dsn_parameters_pass_validation() -> None:
    """An allowlisted parameter must reach the driver, not be rejected here."""
    adapter = PostgresAdapter()
    dsn = Dsn.parse("postgres://u:p@localhost:5432/db?sslmode=require&application_name=registro")

    # Validation passes, so the failure comes from the connection attempt.
    with pytest.raises(AdapterConnectionError, match="connection failed"):
        await adapter.connect(dsn)


@pytest.mark.asyncio
async def test_closing_a_returning_stream_early_keeps_its_writes() -> None:
    """Regression: the display cap / ``--limit`` must not undo DML.

    ``INSERT ... RETURNING`` has already run by the first fetch. The stream used
    to sit in ``async with conn.transaction()``, which rolled back on the
    GeneratorExit of an early close — silently undoing every inserted row while
    the UI showed them as written.
    """
    stmt = _returning_statement([[{"id": 1}], [{"id": 2}]])
    adapter = _adapter_with_statement(stmt)
    tx = adapter._conn.transaction.return_value

    handle = await adapter.execute("INSERT INTO t SELECT 1 RETURNING id")
    stream = await adapter.stream(handle, batch_size=1)
    async for _ in stream:
        break
    await stream.aclose()  # type: ignore[attr-defined]

    tx.commit.assert_awaited_once()
    tx.rollback.assert_not_awaited()
    assert adapter._stream_tx is None


@pytest.mark.asyncio
async def test_a_cancelled_stream_is_rolled_back() -> None:
    stmt = _returning_statement([[{"id": 1}], [{"id": 2}]])
    adapter = _adapter_with_statement(stmt)
    tx = adapter._conn.transaction.return_value

    handle = await adapter.execute("DELETE FROM t RETURNING id")
    stream = await adapter.stream(handle, batch_size=1)
    async for _ in stream:
        adapter._cancel_event(handle).set()
        break
    await stream.aclose()  # type: ignore[attr-defined]

    tx.rollback.assert_awaited_once()
    tx.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_fetch_is_rolled_back_and_reported() -> None:
    stmt = _returning_statement([])
    stmt.cursor.return_value.fetch = AsyncMock(side_effect=asyncpg.PostgresError("boom"))
    adapter = _adapter_with_statement(stmt)
    tx = adapter._conn.transaction.return_value

    handle = await adapter.execute("SELECT id FROM t")
    with pytest.raises(QueryError, match="boom"):
        async for _ in await adapter.stream(handle):
            pass

    tx.rollback.assert_awaited_once()
    tx.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_fully_drained_stream_commits_before_reporting_success() -> None:
    stmt = _returning_statement([[{"id": 1}]])
    adapter = _adapter_with_statement(stmt)
    tx = adapter._conn.transaction.return_value
    seen_commit_before_last: list[bool] = []

    handle = await adapter.execute("SELECT id FROM t")
    async for batch in await adapter.stream(handle):
        if batch.is_last:
            seen_commit_before_last.append(tx.commit.await_count == 1)

    assert seen_commit_before_last == [True]
    assert handle.status is QueryStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_a_commit_failure_is_a_query_error_not_a_success() -> None:
    stmt = _returning_statement([[{"id": 1}]])
    adapter = _adapter_with_statement(stmt)
    tx = adapter._conn.transaction.return_value
    tx.commit = AsyncMock(side_effect=asyncpg.PostgresError("deferred constraint violated"))

    handle = await adapter.execute("INSERT INTO t VALUES (1) RETURNING id")
    with pytest.raises(QueryError, match="deferred constraint"):
        async for _ in await adapter.stream(handle):
            pass
    assert handle.status is not QueryStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_reset_rolls_back_a_stream_that_was_never_closed() -> None:
    stmt = _returning_statement([[{"id": 1}], [{"id": 2}]])
    adapter = _adapter_with_statement(stmt)
    tx = adapter._conn.transaction.return_value

    handle = await adapter.execute("SELECT id FROM t")
    stream = await adapter.stream(handle, batch_size=1)
    await anext(stream)  # abandoned without aclose()
    await adapter.reset()

    tx.rollback.assert_awaited_once()
    assert adapter._stream_tx is None
    # The generator is finalized later; it must not end the transaction twice.
    await stream.aclose()  # type: ignore[attr-defined]
    tx.rollback.assert_awaited_once()
    tx.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_refuses_a_connection_stuck_in_a_transaction() -> None:
    """A connection that cannot leave its transaction must not be pooled."""
    adapter = PostgresAdapter()
    adapter._conn = MagicMock()
    adapter._conn.is_in_transaction.return_value = True
    adapter._conn.execute = AsyncMock(side_effect=asyncpg.InterfaceError("connection lost"))

    with pytest.raises(AdapterError, match="could not reset"):
        await adapter.reset()
