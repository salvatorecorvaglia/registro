"""Vim keymap actually registers its extra bindings at app init."""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from registro_core.config import RegistroConfig, UIConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402

# Vim-only keys that DEFAULT_BINDINGS does not include.
_VIM_ONLY = {"colon", "g", "shift+g"}


@pytest.mark.asyncio
async def test_default_keymap_lacks_vim_bindings() -> None:
    app = RegistroApp(
        config=RegistroConfig(ui=UIConfig(keymap="default")),
        initial_dsn=Dsn.parse("sqlite:///:memory:"),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        keys = set(app._bindings.key_to_bindings.keys())
        assert _VIM_ONLY.isdisjoint(keys), (
            f"vim-only bindings leaked into default keymap: {keys & _VIM_ONLY}"
        )


@pytest.mark.asyncio
async def test_vim_keymap_registers_extra_bindings() -> None:
    app = RegistroApp(
        config=RegistroConfig(ui=UIConfig(keymap="vim")),
        initial_dsn=Dsn.parse("sqlite:///:memory:"),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        keys = set(app._bindings.key_to_bindings.keys())
        missing = _VIM_ONLY - keys
        assert not missing, f"vim keymap did not register: {missing}"


@pytest.mark.asyncio
async def test_gg_is_a_two_key_sequence_not_a_single_g() -> None:
    """Regression: ``g,g`` was handed to Textual, where a comma means "either
    key" — so one ``g`` jumped to the top while the help advertised ``gg``."""
    app = RegistroApp(
        config=RegistroConfig(ui=UIConfig(keymap="vim")),
        initial_dsn=Dsn.parse("sqlite:///:memory:"),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        editor = app._tabs().current_editor
        assert editor is not None
        editor.text = "line1\nline2\nline3"
        editor.move_cursor(editor.document.end)
        end = editor.cursor_location
        # Keys reach the app's bindings only when no editing widget has focus.
        app._results().focus()
        await pilot.pause()

        await pilot.press("g")
        await pilot.pause()
        assert editor.cursor_location == end, "a single g must not jump"

        await pilot.press("g")
        await pilot.pause()
        assert editor.cursor_location == (0, 0)
        assert editor.has_focus


@pytest.mark.asyncio
async def test_a_slow_second_g_starts_over(monkeypatch: pytest.MonkeyPatch) -> None:
    import registro_tui.app as app_module  # noqa: PLC0415

    monkeypatch.setattr(app_module, "_SEQUENCE_TIMEOUT", 0.0)
    app = RegistroApp(
        config=RegistroConfig(ui=UIConfig(keymap="vim")),
        initial_dsn=Dsn.parse("sqlite:///:memory:"),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        editor = app._tabs().current_editor
        assert editor is not None
        editor.text = "a\nb"
        editor.move_cursor(editor.document.end)
        end = editor.cursor_location
        app._results().focus()
        await pilot.pause()

        await pilot.press("g", "g")
        await pilot.pause()
        assert editor.cursor_location == end
