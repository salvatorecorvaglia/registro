"""History modal smoke test."""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from textual.app import App  # noqa: E402
from textual.widgets import ListView, Static  # noqa: E402

from registro_core.config import RegistroConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_engine.history import HistoryEntry  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402
from registro_tui.screens.history import HistoryModal  # noqa: E402


@pytest.mark.asyncio
async def test_history_modal_opens_empty() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.press("ctrl+g")
        await pilot.pause()
        modals = [s for s in app.screen_stack if isinstance(s, HistoryModal)]
        assert modals, "history modal did not appear"
        await pilot.press("escape")
        await pilot.pause()
        assert not any(isinstance(s, HistoryModal) for s in app.screen_stack)


@pytest.mark.asyncio
async def test_history_modal_lists_entry_after_insert() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert app.state.history is not None
        await app.state.history.add(
            HistoryEntry(
                query_id="qx",
                connection_label="sqlite:///:memory:",
                sql="SELECT 'hello from history'",
                status="succeeded",
                duration_ms=1.0,
                rows=1,
            )
        )
        await pilot.press("ctrl+g")
        await pilot.pause()
        modal = next(s for s in app.screen_stack if isinstance(s, HistoryModal))
        listing = modal.query_one("#history-list", ListView)
        assert len(listing.children) >= 1
        await pilot.press("escape")


# ---- HistoryModal.submit ----
#
# The modal's selection path — the code that actually loads a query back into
# the editor, including all three malformed-id guards — was never executed.


def _entry(sql: str, status: str = "succeeded") -> HistoryEntry:
    return HistoryEntry(query_id="q", connection_label="sqlite:///db", sql=sql, status=status)


async def test_submit_returns_the_highlighted_entry() -> None:
    entries = [_entry("SELECT 1"), _entry("SELECT 2")]
    picked: list[HistoryEntry | None] = []

    class _Harness(App[None]):
        pass

    app = _Harness()
    async with app.run_test() as pilot:
        app.push_screen(HistoryModal(entries), picked.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryModal)
        listing = screen.query_one("#history-list", ListView)
        listing.index = 1
        await pilot.pause()
        screen.submit()
        await pilot.pause()

    assert picked == [entries[1]]


async def test_submit_on_an_empty_history_dismisses_with_none() -> None:
    picked: list[HistoryEntry | None] = []

    class _Harness(App[None]):
        pass

    app = _Harness()
    async with app.run_test() as pilot:
        app.push_screen(HistoryModal([]), picked.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryModal)
        screen.submit()
        await pilot.pause()

    assert picked == [None]


async def test_history_preview_is_escaped_and_truncated() -> None:
    """SQL is user data on a markup surface, and long queries must not wrap."""
    long_sql = "SELECT " + "x" * 200
    entries = [_entry("SELECT [/] FROM t"), _entry(long_sql)]

    class _Harness(App[None]):
        pass

    app = _Harness()
    async with app.run_test() as pilot:
        app.push_screen(HistoryModal(entries))
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, HistoryModal)
        labels = [
            str(item.query_one(Static).content) for item in screen.query("#history-list ListItem")
        ]
        assert any("\\[/]" in lbl for lbl in labels), "bracket in SQL must be escaped"
        assert any("…" in lbl for lbl in labels), "long SQL must be truncated"
