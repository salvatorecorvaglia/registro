from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from registro_adapters.sqlite_adapter import SQLiteAdapter
from registro_core.config import RegistroConfig
from registro_core.events import EventBus
from registro_core.models.connection import Dsn
from registro_engine.history import QueryHistory
from registro_tui.session import (
    is_in_memory,
    is_single_connection_dsn,
    open_session,
    share_in_memory_sqlite,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("dsn_str", "expected"),
    [
        ("sqlite:///:memory:", True),
        ("sqlite://", True),
        ("sqlite:///./data.db", False),
        ("duckdb:///:memory:", True),
        ("duckdb://", True),
        ("duckdb:///./data.duckdb", False),
        ("postgres://localhost/db", False),
        ("mysql://localhost/db", False),
    ],
)
def test_is_in_memory(dsn_str: str, expected: bool) -> None:
    assert is_in_memory(Dsn.parse(dsn_str)) is expected


@pytest.mark.parametrize(
    ("dsn_str", "expected"),
    [
        ("duckdb:///:memory:", True),
        ("duckdb://", True),
        ("duckdb:///./data.duckdb", False),
        ("sqlite:///:memory:", False),  # sqlite has a shared-cache path instead
        ("postgres://localhost/db", False),
    ],
)
def test_is_single_connection_dsn(dsn_str: str, expected: bool) -> None:
    assert is_single_connection_dsn(Dsn.parse(dsn_str)) is expected


def test_share_in_memory_sqlite_rewrites_to_a_named_shared_cache_uri() -> None:
    dsn = Dsn.parse("sqlite:///:memory:")
    shared = share_in_memory_sqlite(dsn)
    assert shared.scheme == "sqlite"
    assert shared.database is not None
    assert shared.database.startswith("file:registro-mem-")
    assert shared.params["mode"] == "memory"
    assert shared.params["cache"] == "shared"


def test_share_in_memory_sqlite_is_idempotent_across_calls() -> None:
    dsn = Dsn.parse("sqlite:///:memory:")
    first = share_in_memory_sqlite(dsn)
    second = share_in_memory_sqlite(dsn)
    # Each call mints a fresh unique name — different in-memory databases,
    # not the same one reused.
    assert first.database != second.database


def test_share_in_memory_sqlite_leaves_a_real_file_path_untouched() -> None:
    dsn = Dsn.parse("sqlite:///./data.db")
    assert share_in_memory_sqlite(dsn) == dsn


def test_share_in_memory_sqlite_leaves_non_sqlite_dsns_untouched() -> None:
    dsn = Dsn.parse("duckdb:///:memory:")
    assert share_in_memory_sqlite(dsn) == dsn


# ---- open_session partial failure, Session.close resilience ----
#
# Both are explicitly leak-prevention code, and neither was executed.


async def test_open_session_closes_what_it_opened_when_a_later_step_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A partial connect must not leak a live connection or a history handle."""
    opened: list[object] = []
    closed: list[object] = []

    async def _boom(self: QueryHistory) -> None:
        # Fail at the last step, after the adapter and pool already exist.
        raise OSError("history store unavailable")

    monkeypatch.setattr(QueryHistory, "open", _boom)

    real_connect = SQLiteAdapter.connect
    real_close = SQLiteAdapter.close

    async def _tracking_connect(self: SQLiteAdapter, dsn: Dsn) -> None:
        await real_connect(self, dsn)
        opened.append(self)

    async def _tracking_close(self: SQLiteAdapter) -> None:
        closed.append(self)
        await real_close(self)

    monkeypatch.setattr(SQLiteAdapter, "connect", _tracking_connect)
    monkeypatch.setattr(SQLiteAdapter, "close", _tracking_close)

    cfg = RegistroConfig()
    cfg.engine.history_path = str(tmp_path / "history.db")
    bus = EventBus()
    try:
        with pytest.raises(OSError, match="history store unavailable"):
            await open_session(Dsn.parse("sqlite:///:memory:"), cfg, bus)
    finally:
        await bus.close()

    assert opened, "the adapter should have been connected before the failure"
    assert set(closed) >= set(opened), "a partial connect leaked an open adapter"


async def test_session_close_continues_past_a_failing_resource(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One resource refusing to close must not strand the others."""
    cfg = RegistroConfig()
    cfg.engine.history_path = str(tmp_path / "history.db")
    bus = EventBus()
    session = await open_session(Dsn.parse("sqlite:///:memory:"), cfg, bus)
    try:

        async def _boom() -> None:
            raise RuntimeError("history refuses to close")

        monkeypatch.setattr(session.history, "close", _boom)

        await session.close()  # must not raise

        assert not session.adapter.connected, "the adapter should still have been closed"
    finally:
        await bus.close()
