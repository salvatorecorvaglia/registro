"""Help modal smoke test."""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from registro_core.config import RegistroConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402
from registro_tui.keymaps import GLOBAL_BINDINGS, key_label  # noqa: E402
from registro_tui.screens.help import HelpModal, _rows  # noqa: E402


@pytest.mark.asyncio
async def test_help_modal_opens_and_dismisses() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.press("question_mark")
        await pilot.pause()
        # The modal should now be on the screen stack.
        assert any(isinstance(s, HelpModal) for s in app.screen_stack)
        await pilot.press("escape")
        await pilot.pause()
        assert not any(isinstance(s, HelpModal) for s in app.screen_stack)


# ---- _render_bindings ----
#
# The modal was only ever opened and dismissed; the code that builds the list
# was untested, which is how it came to show raw `ctrl+enter` while the footer
# two lines below showed `^↵`.


def test_rows_collapse_aliased_bindings_onto_one_line() -> None:
    rows = _rows(
        [
            ("ctrl+enter", "run_query", "Run query"),
            ("ctrl+j", "run_query", "Run query"),
            ("ctrl+q", "quit", "Quit"),
        ]
    )
    assert rows == [("^↵ / ^j", "Run query"), ("^q", "Quit")]


def test_rows_render_keys_the_way_the_footer_does() -> None:
    """One spelling of a binding across the whole UI."""
    for key, _action, _desc in GLOBAL_BINDINGS:
        assert key_label(key) in " ".join(k for k, _ in _rows(GLOBAL_BINDINGS))


def test_rows_deduplicate_a_repeated_key() -> None:
    rows = _rows([("ctrl+q", "quit", "Quit"), ("ctrl+q", "quit", "Quit")])
    assert rows == [("^q", "Quit")]


async def test_help_lists_global_and_results_bindings() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        modal = HelpModal()
        app.push_screen(modal)
        await pilot.pause()

        text = modal._render_bindings()
        assert "Global" in text
        assert "Results pane" in text
        # Results-pane actions must still be discoverable even though they are
        # no longer app-level bindings.
        assert "Copy cell" in text
        assert "Export CSV" in text
        # Spelled like the footer, not like Textual's binding strings.
        assert "ctrl+enter" not in text
        assert "^↵" in text


async def test_help_shows_vim_bindings_only_in_vim_mode() -> None:
    plain = RegistroConfig()
    vim = RegistroConfig()
    vim.ui.keymap = "vim"

    for cfg, expected in ((plain, False), (vim, True)):
        app = RegistroApp(config=cfg, initial_dsn=Dsn.parse("sqlite:///:memory:"))
        async with app.run_test() as pilot:
            await pilot.pause()
            await app.workers.wait_for_complete()
            modal = HelpModal()
            app.push_screen(modal)
            await pilot.pause()
            assert ("Vim navigation" in modal._render_bindings()) is expected
