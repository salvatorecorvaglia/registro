"""Smoke test: app boots, mounts, and can be torn down."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

textual = pytest.importorskip("textual")

from textual.content import Content  # noqa: E402
from textual.widgets import ListView, Static  # noqa: E402

from registro_core.config import RegistroConfig, UIConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_core.protocols.plugin import PluginManifest  # noqa: E402
from registro_plugins.api import Command  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402
from registro_tui.screens.command_palette import CommandPaletteScreen  # noqa: E402

_RECURSIVE_BUSY_SQL = (
    "WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM t LIMIT 200000) SELECT * FROM t"
)


@pytest.mark.asyncio
async def test_app_boots_in_memory_sqlite() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        # Give startup tasks a chance to settle.
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert app.state.adapter is not None
        assert app.state.adapter.connected  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_query_rejects_reentry_while_in_flight() -> None:
    # Regression: a second run while one is in flight must not clobber the
    # current run handle — it should be rejected up front.
    app = RegistroApp(config=RegistroConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        app._query_in_flight = True
        await app.action_run_query()
        await app.workers.wait_for_complete()
        assert "already running" in app._status().message


@pytest.mark.asyncio
async def test_run_query_empty_sql_gives_feedback() -> None:
    # Running with an empty editor must not silently no-op.
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await app.action_run_query()
        await app.workers.wait_for_complete()
        assert "nothing to run" in app._status().message


@pytest.mark.asyncio
async def test_closing_last_tab_keeps_an_editor() -> None:
    # Closing every tab must leave a usable editor behind.
    app = RegistroApp(config=RegistroConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_close_tab()
        await pilot.pause()
        assert app._tabs().current_editor is not None


@pytest.mark.asyncio
async def test_focus_editor_actions_move_cursor() -> None:
    app = RegistroApp(config=RegistroConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        editor = app._tabs().current_editor
        assert editor is not None
        editor.text = "line1\nline2\nline3"
        await app.action_focus_editor_bottom()
        assert editor.cursor_location == editor.document.end
        await app.action_focus_editor_top()
        assert editor.cursor_location == (0, 0)


@pytest.mark.asyncio
async def test_teardown_closes_connection() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        adapter = app.state.adapter
        assert adapter is not None
        assert adapter.connected  # type: ignore[attr-defined]
    # on_unmount must have torn the connection down.
    assert adapter.connected is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_command_palette_invokes_with_correct_plugin_context() -> None:
    app = RegistroApp(config=RegistroConfig())
    resolved_context = None

    class CustomCommandPlugin:
        manifest = PluginManifest(id="custom_cmd_plugin", name="CustomCmd", version="1.0.0")

        def on_load(self, ctx: Any) -> None:
            async def _handler(c: Any) -> None:
                nonlocal resolved_context
                resolved_context = c

            ctx.register_command(Command(id="custom.cmd", title="Run Custom", handler=_handler))

        def on_unload(self, ctx: Any) -> None:
            pass

    async with app.run_test() as pilot:
        await pilot.pause()
        app._loader.load_instance(CustomCommandPlugin())

        # Trigger command palette
        await app.action_command_palette()
        await pilot.pause()

        # Find the command screen and pick our command
        screen = app.screen
        assert isinstance(screen, CommandPaletteScreen)

        lst = screen.query_one("#palette-list", ListView)
        for i, child in enumerate(lst.children):
            cmd = getattr(child, "cmd", None)
            if cmd is not None and cmd.id == "custom.cmd":
                lst.index = i
                break
        else:
            pytest.fail("Custom command option not found in command palette")

        # Press enter to pick the highlighted command
        await pilot.press("enter")
        await pilot.pause()

        # Check if resolved context matches the plugin context
        assert resolved_context is not None
        assert resolved_context.plugin_id == "custom_cmd_plugin"


@pytest.mark.asyncio
async def test_action_bar_unlimited_limit_label() -> None:
    config = RegistroConfig(ui=UIConfig(max_display_rows=0))
    app = RegistroApp(config=config)
    async with app.run_test() as pilot:
        await pilot.pause()
        limit_label = app.query_one("#limit-label", Static)
        assert "Display cap Unlimited" in str(limit_label.render())


@pytest.mark.asyncio
async def test_copy_cell_with_nothing_selected_gives_feedback() -> None:
    app = RegistroApp(config=RegistroConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_copy_cell()
        assert "nothing to copy" in app._status().message


@pytest.mark.asyncio
async def test_cancel_query_stops_an_in_flight_run() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()

        editor = app._tabs().current_editor
        assert editor is not None
        editor.text = _RECURSIVE_BUSY_SQL

        # Starting the run no longer blocks: it hands the stream to a worker so
        # the message pump stays free (that is what makes the cancel key work at
        # all — see tests/tui/test_keymap_conflicts.py).
        await app.action_run_query()
        for _ in range(200):
            if app._current_run is not None:
                break
            await asyncio.sleep(0.005)
        assert app._current_run is not None

        await app.action_cancel_query()
        await asyncio.wait_for(app.workers.wait_for_complete(), timeout=5)

        # Assert on the rendered text, not the markup source: the severity
        # colour is a theme variable now, and pinning the literal string made
        # this a test of the palette rather than of the cancellation.
        assert "cancelled" in Content.from_markup(app._status().message).plain
        assert not app._query_in_flight


@pytest.mark.asyncio
async def test_unmount_cancels_an_in_flight_run() -> None:
    # Exercises the `on_unmount` branch directly: a query in flight must be
    # signalled to cancel during teardown, not left to run past it. A fake
    # stand-in for `action_cancel_query` avoids racing the real query stream
    # against the widget tree being torn down by the same `run_test()` exit.
    app = RegistroApp(config=RegistroConfig())
    cancelled = asyncio.Event()

    async def _fake_cancel_query() -> None:
        cancelled.set()

    async with app.run_test() as pilot:
        await pilot.pause()
        app._current_run = object()  # type: ignore[assignment]
        app.action_cancel_query = _fake_cancel_query  # type: ignore[method-assign]

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_unmount_does_not_cancel_when_no_run_is_in_flight() -> None:
    app = RegistroApp(config=RegistroConfig())
    cancel_called = asyncio.Event()

    async def _fake_cancel_query() -> None:
        cancel_called.set()

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._current_run is None
        app.action_cancel_query = _fake_cancel_query  # type: ignore[method-assign]

    assert not cancel_called.is_set()


@pytest.mark.asyncio
async def test_unmount_cancels_an_in_flight_palette_task() -> None:
    app = RegistroApp(config=RegistroConfig())
    started = asyncio.Event()
    task_holder: dict[str, asyncio.Task[None]] = {}

    class SlowCommandPlugin:
        manifest = PluginManifest(id="slow_cmd_plugin", name="SlowCmd", version="1.0.0")

        def on_load(self, ctx: Any) -> None:
            async def _handler(_ctx: Any) -> None:
                started.set()
                await asyncio.sleep(60)

            ctx.register_command(Command(id="slow.cmd", title="Slow", handler=_handler))

        def on_unload(self, ctx: Any) -> None:
            pass

    async with app.run_test() as pilot:
        await pilot.pause()
        app._loader.load_instance(SlowCommandPlugin())

        await app.action_command_palette()
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, CommandPaletteScreen)
        lst = screen.query_one("#palette-list", ListView)
        for i, child in enumerate(lst.children):
            cmd = getattr(child, "cmd", None)
            if cmd is not None and cmd.id == "slow.cmd":
                lst.index = i
                break
        else:
            pytest.fail("Slow command option not found in command palette")

        await pilot.press("enter")
        await pilot.pause()
        await asyncio.wait_for(started.wait(), timeout=1)

        task = app._palette_task
        assert task is not None
        assert not task.done()
        task_holder["task"] = task

    # Exiting run_test() triggers on_unmount, which must cancel the
    # still-running palette task rather than leave it dangling.
    assert task_holder["task"].cancelled()
