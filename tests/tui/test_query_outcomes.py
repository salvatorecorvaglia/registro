"""How a finished query is reported: status bar, toasts, and history."""

from __future__ import annotations

import asyncio

import pytest

textual = pytest.importorskip("textual")

from registro_core.config import RegistroConfig, UIConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402

_FIFTY_ROWS = (
    "WITH RECURSIVE t(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM t WHERE x < 50) SELECT x FROM t"
)


async def _run(app: RegistroApp, pilot: object, sql: str) -> None:
    editor = app._tabs().current_editor
    assert editor is not None
    editor.text = sql
    await app.action_run_query()
    await app.workers.wait_for_complete()
    # Bus subscribers run on their own tasks; give them a turn.
    for _ in range(5):
        await pilot.pause()  # type: ignore[attr-defined]
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_display_capped_run_is_a_success_not_a_cancellation() -> None:
    """Hitting the display cap used to be reported as a cancelled query.

    The history said "cancelled", the status bar never received the run's
    completion, and the only hint it had succeeded was the grid itself.
    """
    cfg = RegistroConfig(ui=UIConfig(max_display_rows=10, row_batch_size=10))
    app = RegistroApp(config=cfg, initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await _run(app, pilot, _FIFTY_ROWS)

        results = app._results()
        assert results.truncated
        assert results.row_count == 10
        assert "cancelled" not in results.summary

        bar = app._status()
        assert bar.has_run, "QueryCompleted never reached the status bar"
        assert bar.truncated

        assert app.state.history is not None
        [entry] = await app.state.history.recent(limit=1)
        assert entry.status == "succeeded"


@pytest.mark.asyncio
async def test_a_query_error_is_reported_exactly_once_and_readably() -> None:
    """One failure, one toast, and no internal code or SQL dump in it.

    The executor's ``QueryFailed`` subscriber and ``_stream_query`` both used
    to report the same error, and both rendered ``str(exc)`` — ``[registro.query]
    msg :: {'sql': '<the whole query>'}``.
    """
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await _run(app, pilot, "SELEC 1")

        notifications = list(app._notifications)
        assert len(notifications) == 1, [n.message for n in notifications]
        [toast] = notifications
        assert toast.title == "Query failed"
        assert "syntax error" in toast.message
        assert "registro.query" not in toast.message
        assert "'sql'" not in toast.message


@pytest.mark.asyncio
async def test_a_schema_refresh_failure_is_reported_not_fatal(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An unexpected exception in the connect worker used to exit the app."""
    from registro_engine.introspector import Introspector  # noqa: PLC0415

    async def _explode(self, *, lazy: bool = False):  # type: ignore[no-untyped-def]
        raise RuntimeError("catalog view vanished")

    monkeypatch.setattr(Introspector, "refresh", _explode)
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert app.is_running
        assert "catalog view vanished" in app._status().message


@pytest.mark.asyncio
async def test_dml_reports_rows_affected_not_no_rows_returned() -> None:
    """An UPDATE that changed rows used to be captioned "no rows returned"."""
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        results = app._results()

        await _run(app, pilot, "CREATE TABLE t (id INTEGER)")
        assert results.summary == "statement executed"

        await _run(app, pilot, "INSERT INTO t VALUES (1), (2), (3)")
        assert results.summary == "3 rows affected"

        await _run(app, pilot, "UPDATE t SET id = id + 10 WHERE id = 1")
        assert results.summary == "1 row affected"

        await _run(app, pilot, "SELECT id FROM t WHERE id < 0")
        assert results.summary == "no rows returned"

        await _run(app, pilot, "SELECT nope FROM t")
        assert results.summary == "query failed"


@pytest.mark.asyncio
async def test_failed_runs_are_recorded_with_their_error_and_query_id() -> None:
    """History stored neither why a query failed nor, for a failure before
    streaming began, the query id the executor had already published."""
    from registro_core.events import QueryFailed  # noqa: PLC0415

    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        published: list[QueryFailed] = []

        async def _grab(ev: QueryFailed) -> None:
            published.append(ev)

        app.state.bus.subscribe(QueryFailed, _grab)
        await _run(app, pilot, "SELECT nope FROM missing_table")

        assert app.state.history is not None
        [entry] = await app.state.history.recent(limit=1)
        assert entry.status == "failed"
        assert entry.error is not None
        assert "missing_table" in entry.error
        assert "registro.query" not in entry.error
        assert published
        assert entry.query_id == published[0].query_id


@pytest.mark.asyncio
async def test_the_history_modal_shows_why_a_query_failed() -> None:
    from textual.app import App  # noqa: PLC0415
    from textual.widgets import Static  # noqa: PLC0415

    from registro_engine.history import HistoryEntry  # noqa: PLC0415
    from registro_tui.screens.history import HistoryModal  # noqa: PLC0415

    entry = HistoryEntry(
        query_id="q1",
        connection_label="sqlite://",
        sql="SELECT nope",
        status="failed",
        error="no such column: nope\nsecond line",
    )

    class _Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(HistoryModal([entry]))

    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        texts = [str(s.content) for s in app.screen.query(Static)]
        assert any("no such column: nope" in t for t in texts)
        assert not any("second line" in t for t in texts)


@pytest.mark.asyncio
async def test_only_the_selection_runs_when_there_is_one() -> None:
    from textual.widgets.text_area import Selection  # noqa: PLC0415

    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        editor = app._tabs().current_editor
        assert editor is not None
        editor.text = "SELECT 1 AS first;\nSELECT 2 AS second;"
        editor.selection = Selection((1, 0), (1, 18))
        await app.action_run_query()
        await app.workers.wait_for_complete()
        await pilot.pause()

        results = app._results()
        assert [str(c.label) for c in results.columns.values()] == ["second"]


@pytest.mark.asyncio
async def test_a_multi_statement_script_error_says_how_to_run_it() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await _run(app, pilot, "SELECT 1; SELECT 2")

        [toast] = list(app._notifications)
        assert "one statement at a time" in toast.message
        assert "Select a single statement" in toast.message


@pytest.mark.asyncio
async def test_an_explicit_begin_warns_that_it_will_not_be_kept() -> None:
    """Every run's connection is rolled back on release, so BEGIN ... COMMIT
    across runs silently autocommits the statements in between."""
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await _run(app, pilot, "-- open one\nBEGIN")

        titles = [n.title for n in app._notifications]
        assert "Explicit transactions are not kept" in titles


@pytest.mark.asyncio
async def test_ctrl_l_opens_the_row_limit_dialog_and_unlimited_warns() -> None:
    from textual.widgets import Input  # noqa: PLC0415

    from registro_tui.screens.row_limit import RowLimitModal  # noqa: PLC0415

    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.press("ctrl+l")
        await pilot.pause()
        assert isinstance(app.screen, RowLimitModal)

        app.screen.query_one("#limit-input", Input).value = "0"
        await pilot.press("enter")
        await pilot.pause()

        assert app.cfg.ui.max_display_rows == 0
        assert "Row display limit removed" in [n.title for n in app._notifications]
