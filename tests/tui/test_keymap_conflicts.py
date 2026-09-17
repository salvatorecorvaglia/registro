"""App-level bindings must not be shadowed by the widget that has focus.

Textual resolves a key from the focused widget upward, so an app-level binding
loses to any binding the focused widget declares for the same key. Registro is a
SQL editor: the editor has focus most of the time, and ``TextArea`` claims a
generous slice of the ``ctrl+`` space. Four Registro bindings sat squarely on
top of it — ``ctrl+c`` (cancel query vs. copy), ``ctrl+e`` (export vs. line
end), ``ctrl+w`` (close tab vs. delete word), ``ctrl+y`` (copy cell vs. redo) —
so those keys silently did the editor's thing instead, while the help modal
explicitly documented the opposite for ``ctrl+c``.

This asserts the property against the *installed* Textual rather than a list
copied into a comment, so a future Textual that claims another key fails here
instead of in a user's terminal.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import DataTable, Input, TextArea  # noqa: E402

from registro_tui.keymaps.default import (  # noqa: E402
    GLOBAL_BINDINGS,
    RESULTS_BINDINGS,
)
from registro_tui.keymaps.vim import VIM_BINDINGS  # noqa: E402


def _declared_keys(widget_cls: type) -> set[str]:
    """Every key ``widget_cls`` binds, splitting Textual's comma-joined form.

    Textual writes multi-key bindings as one string (``"ctrl+c,super+c"``,
    ``"end,ctrl+e"``), so a naive membership test misses exactly the conflicts
    that matter.
    """
    keys: set[str] = set()
    for binding in getattr(widget_cls, "BINDINGS", []):
        raw = getattr(binding, "key", None)
        if raw is None and isinstance(binding, tuple):
            raw = binding[0]
        elif raw is None:
            raw = binding
        keys.update(part.strip() for part in str(raw).split(",") if part.strip())
    return keys


# Widgets that can hold focus while a *global* binding needs to fire. The SQL
# editor is a TextArea; modals and the connection dialog use Input.
_EDITING_WIDGETS = (TextArea, Input)

# Widgets on the results grid's own focus chain.
_RESULTS_WIDGETS = (DataTable,)


@pytest.mark.parametrize(("key", "action", "_desc"), GLOBAL_BINDINGS)
def test_global_binding_is_not_shadowed_by_an_editing_widget(
    key: str, action: str, _desc: str
) -> None:
    for widget_cls in _EDITING_WIDGETS:
        claimed = _declared_keys(widget_cls)
        assert key not in claimed, (
            f"{key!r} ({action}) is also bound by {widget_cls.__name__}; "
            f"the focused widget wins, so this app binding would never fire "
            f"while that widget has focus. Pick another key, or move the "
            f"binding onto the widget that owns the action."
        )


@pytest.mark.parametrize(("key", "action", "_desc"), RESULTS_BINDINGS)
def test_results_binding_is_not_shadowed_on_its_own_focus_chain(
    key: str, action: str, _desc: str
) -> None:
    for widget_cls in _RESULTS_WIDGETS:
        claimed = _declared_keys(widget_cls)
        assert key not in claimed, (
            f"{key!r} ({action}) is also bound by {widget_cls.__name__}, "
            f"which is on the results grid's own focus chain."
        )


@pytest.mark.parametrize(("key", "action", "_desc"), VIM_BINDINGS)
def test_vim_binding_is_not_shadowed_by_an_editing_widget(
    key: str, action: str, _desc: str
) -> None:
    for widget_cls in _EDITING_WIDGETS:
        assert key not in _declared_keys(widget_cls), (
            f"vim binding {key!r} ({action}) is shadowed by {widget_cls.__name__}"
        )


def test_the_two_scopes_do_not_overlap() -> None:
    """A key must mean one thing. Overlap would make behaviour focus-dependent."""
    global_keys = {k for k, _, _ in GLOBAL_BINDINGS}
    results_keys = {k for k, _, _ in RESULTS_BINDINGS}
    assert not (global_keys & results_keys)


def test_every_action_is_reachable_from_the_keymap() -> None:
    """Guards against a binding that names an action the app does not implement."""
    from registro_tui.app import RegistroApp  # noqa: PLC0415

    for key, action, _desc in (*GLOBAL_BINDINGS, *RESULTS_BINDINGS):
        assert hasattr(RegistroApp, f"action_{action}"), (
            f"{key!r} is bound to {action!r}, but RegistroApp has no action_{action}"
        )


# ---- end-to-end: the keys must actually fire in a running app ----


async def test_cancel_key_reaches_the_app_while_the_editor_has_focus() -> None:
    """The regression this whole module exists for.

    With the SQL editor focused, pressing the cancel key must invoke the app's
    action rather than the editor's clipboard copy. Patched on the instance,
    not via a subclass: ``CSS_PATH`` resolves relative to the defining module,
    so a subclass declared in a test file cannot find the stylesheet.
    """
    from registro_core.config import RegistroConfig  # noqa: PLC0415
    from registro_core.models.connection import Dsn  # noqa: PLC0415
    from registro_tui.app import RegistroApp  # noqa: PLC0415
    from registro_tui.keymaps.default import ACTION_TO_KEY  # noqa: PLC0415
    from registro_tui.widgets.sql_editor import SqlEditor  # noqa: PLC0415

    fired = False

    async def _fake_cancel() -> None:
        nonlocal fired
        fired = True

    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        app.action_cancel_query = _fake_cancel  # type: ignore[method-assign]
        editor = app.query(SqlEditor).first()
        editor.focus()
        await pilot.pause()
        assert app.focused is editor, "editor must hold focus for this to mean anything"

        await pilot.press(ACTION_TO_KEY["cancel_query"])
        await pilot.pause()

    assert fired, "cancel key did not reach the app while the editor had focus"


async def test_results_bindings_bubble_to_the_app_actions() -> None:
    """Bindings declared on ResultsTable resolve against the app's action methods."""
    from registro_core.config import RegistroConfig  # noqa: PLC0415
    from registro_core.models.connection import Dsn  # noqa: PLC0415
    from registro_tui.app import RegistroApp  # noqa: PLC0415
    from registro_tui.keymaps.default import RESULTS_BINDINGS  # noqa: PLC0415
    from registro_tui.widgets.results_table import ResultsTable  # noqa: PLC0415

    fired: list[str] = []

    async def _fake_copy() -> None:
        fired.append("copy_cell")

    async def _fake_export() -> None:
        fired.append("export_csv")

    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        app.action_copy_cell = _fake_copy  # type: ignore[method-assign]
        app.action_export_csv = _fake_export  # type: ignore[method-assign]
        table = app.query(ResultsTable).first()
        table.focus()
        await pilot.pause()
        assert app.focused is table

        for key, _action, _desc in RESULTS_BINDINGS:
            await pilot.press(key)
            await pilot.pause()

    assert fired == [action for _k, action, _d in RESULTS_BINDINGS]


async def test_cancel_key_dispatches_while_a_query_is_streaming() -> None:
    """The cancel key must reach the app *during* the query it aborts.

    A binding's action is awaited by the App's own message pump. Draining the
    result stream inside ``action_run_query`` therefore occupied that pump for
    the whole query, and no further key could be dispatched until it finished —
    so the cancel key was inert during exactly the long query it exists for.
    Queries launched from the Run *button* were unaffected (a Button.Pressed
    handler runs on the button's pump), which made the two entry points behave
    differently.

    The stream is parked mid-flight here rather than relying on a query that
    happens to be slow, so the test states the property instead of racing it.
    """
    from registro_core.config import RegistroConfig  # noqa: PLC0415
    from registro_core.models.connection import Dsn  # noqa: PLC0415
    from registro_tui.app import RegistroApp  # noqa: PLC0415
    from registro_tui.keymaps.default import ACTION_TO_KEY  # noqa: PLC0415
    from registro_tui.widgets.results_table import ResultsTable  # noqa: PLC0415
    from registro_tui.widgets.sql_editor import SqlEditor  # noqa: PLC0415

    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()

        streaming = asyncio.Event()
        release = asyncio.Event()
        table = app.query(ResultsTable).first()
        original_feed = table.feed

        async def _parked_feed(batch: object) -> None:
            streaming.set()
            await release.wait()
            await original_feed(batch)  # type: ignore[arg-type]

        table.feed = _parked_feed  # type: ignore[method-assign, assignment]

        cancelled = asyncio.Event()

        async def _fake_cancel() -> None:
            cancelled.set()

        app.action_cancel_query = _fake_cancel  # type: ignore[method-assign]

        editor = app.query(SqlEditor).first()
        editor.text = (
            "WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM t LIMIT 50000) "
            "SELECT * FROM t"
        )
        editor.focus()
        await pilot.pause()

        press_run = asyncio.create_task(pilot.press(ACTION_TO_KEY["run_query"]))
        await asyncio.wait_for(streaming.wait(), timeout=5)

        # The query is now parked mid-stream. If the pump is free, this key is
        # dispatched; if the query action owns the pump, it waits behind it.
        press_cancel = asyncio.create_task(pilot.press(ACTION_TO_KEY["cancel_query"]))
        dispatched = True
        try:
            await asyncio.wait_for(cancelled.wait(), timeout=2)
        except TimeoutError:
            dispatched = False

        # Unpark and drain before asserting, so a failure reports the property
        # under test rather than whatever teardown trips over first.
        release.set()
        for task in (press_run, press_cancel):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await asyncio.wait_for(app.workers.wait_for_complete(), timeout=10)

        assert dispatched, (
            "cancel key was not dispatched while the query was streaming — "
            "the App message pump is blocked by the query action"
        )
