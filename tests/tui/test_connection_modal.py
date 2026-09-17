"""Connection dialog behaviour.

The dialog was the least-covered module in the TUI (38%), which is how the
MarkupError on a bracket-bearing DSN reached a release: nothing exercised
``submit`` at all. The preset list, the up/down focus handoff between the input
and the presets, and every rejection path are covered here.
"""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from textual.app import App, ComposeResult  # noqa: E402
from textual.widgets import Button, Input, ListView, Static  # noqa: E402

from registro_tui.screens.connection import _PRESETS, ConnectionScreen  # noqa: E402


class _Harness(App[None]):
    # A composed default screen, matching the shape of the real app. A bare
    # App leaves an empty screen underneath the modal, and dismissing on
    # ``escape`` then races the test harness's own teardown pop.
    def compose(self) -> ComposeResult:
        yield Static("base")


async def _open() -> tuple[_Harness, list[str | None]]:
    app = _Harness()
    results: list[str | None] = []
    return app, results


async def test_valid_dsn_is_returned() -> None:
    app, results = await _open()
    async with app.run_test() as pilot:
        app.push_screen(ConnectionScreen(), results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConnectionScreen)
        screen.query_one("#conn-input", Input).value = "duckdb:///:memory:"
        screen.submit()
        await pilot.pause()
    assert results == ["duckdb:///:memory:"]


@pytest.mark.parametrize(
    ("value", "expected_fragment"),
    [
        ("", "Enter a DSN"),
        ("   ", "Enter a DSN"),
        ("oracle://host/db", "unsupported scheme"),
        ("not-a-dsn", "missing scheme"),
        ("postgres://host:notaport/db", "invalid DSN"),
    ],
)
async def test_invalid_input_reports_inline_and_keeps_the_modal_open(
    value: str, expected_fragment: str
) -> None:
    """Rejection must say why, in the field that holds the mistake.

    A bare ``return`` made Enter look like a dead key.
    """
    app, results = await _open()
    async with app.run_test() as pilot:
        app.push_screen(ConnectionScreen(), results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConnectionScreen)
        screen.query_one("#conn-input", Input).value = value
        screen.submit()
        await pilot.pause()

        assert isinstance(app.screen, ConnectionScreen), "modal should stay open"
        error = screen.query_one("#conn-error", Static).content
        assert expected_fragment in error
    assert results == []


async def test_escape_cancels_with_none() -> None:
    app, results = await _open()
    async with app.run_test() as pilot:
        app.push_screen(ConnectionScreen(), results.append)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert results == [None]


async def test_selecting_a_preset_populates_and_submits() -> None:
    app, results = await _open()
    async with app.run_test() as pilot:
        app.push_screen(ConnectionScreen(), results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConnectionScreen)
        presets = screen.query_one("#conn-presets", ListView)
        presets.index = 1
        presets.action_select_cursor()
        await pilot.pause()
    assert results == [_PRESETS[1][1]]


async def test_down_moves_focus_from_the_input_to_the_presets_and_back() -> None:
    app, _ = await _open()
    async with app.run_test() as pilot:
        app.push_screen(ConnectionScreen())
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConnectionScreen)
        field = screen.query_one("#conn-input", Input)
        presets = screen.query_one("#conn-presets", ListView)

        assert field.has_focus
        await pilot.press("down")
        await pilot.pause()
        assert presets.has_focus

        presets.index = 0
        await pilot.press("up")
        await pilot.pause()
        assert field.has_focus


async def test_the_connect_button_submits() -> None:
    app, results = await _open()
    async with app.run_test() as pilot:
        app.push_screen(ConnectionScreen(), results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConnectionScreen)
        screen.query_one("#conn-input", Input).value = "sqlite:///:memory:"
        screen.query_one("#conn-submit-btn", Button).press()
        await pilot.pause()
    assert results == ["sqlite:///:memory:"]


async def test_enter_defers_to_the_preset_list_when_it_has_focus() -> None:
    """Otherwise the modal would submit twice for one keystroke."""
    app, _ = await _open()
    async with app.run_test() as pilot:
        app.push_screen(ConnectionScreen())
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConnectionScreen)
        assert screen.handles_enter_itself() is False
        screen.query_one("#conn-presets", ListView).focus()
        await pilot.pause()
        assert screen.handles_enter_itself() is True


async def test_escape_does_not_crash_the_real_app() -> None:
    """Regression: escape on the connection dialog raised ScreenStackError.

    Textual delivers one key press to the screen twice — the focused widget
    bubbles it up, and the screen is offered it directly — so ``escape``
    reached ``cancel()`` a second time after the modal had already been popped,
    and the second ``dismiss`` popped an empty stack. This is the dialog the
    app opens on startup when no DSN is given, so it was the very first
    keystroke a new user might try.
    """
    from registro_core.config import RegistroConfig  # noqa: PLC0415
    from registro_tui.app import RegistroApp  # noqa: PLC0415

    app = RegistroApp(config=RegistroConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ConnectionScreen), "startup should open the dialog"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ConnectionScreen)


@pytest.mark.parametrize("repeats", [2, 3])
async def test_resolving_a_modal_twice_is_a_no_op(repeats: int) -> None:
    """Every modal inherits an idempotent dismissal, not just this one."""
    app, results = await _open()
    async with app.run_test() as pilot:
        app.push_screen(ConnectionScreen(), results.append)
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConnectionScreen)
        for _ in range(repeats):
            screen.cancel()
        await pilot.pause()
    assert results == [None], "only the first resolution should reach the callback"
