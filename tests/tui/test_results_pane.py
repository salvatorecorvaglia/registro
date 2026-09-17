"""Tests for ResultsTable empty-state, loading reset, copy, and export paths."""

from __future__ import annotations

import csv

import pytest

textual = pytest.importorskip("textual")

from registro_core.config import RegistroConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402


@pytest.mark.asyncio
async def test_query_with_rows_sets_count_subtitle(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        editor = app._tabs().current_editor
        assert editor is not None
        editor.text = "SELECT 1 AS a UNION ALL SELECT 2"
        await app.action_run_query()
        # ``action_run_query`` now starts a worker and returns, so the pump
        # stays free for the cancel key. Wait on the worker rather than
        # hoping a single ``pause()`` outlasts the query.
        await app.workers.wait_for_complete()
        await pilot.pause()
        results = app._results()
        assert results.loading is False
        assert results.row_count == 2
        assert results.border_subtitle == "2 rows"


@pytest.mark.asyncio
async def test_query_with_no_rows_sets_empty_subtitle() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        editor = app._tabs().current_editor
        assert editor is not None
        editor.text = "SELECT 1 WHERE 0"
        await app.action_run_query()
        # ``action_run_query`` now starts a worker and returns, so the pump
        # stays free for the cancel key. Wait on the worker rather than
        # hoping a single ``pause()`` outlasts the query.
        await app.workers.wait_for_complete()
        await pilot.pause()
        results = app._results()
        assert results.loading is False
        assert results.row_count == 0
        assert results.border_subtitle == "no rows returned"


@pytest.mark.asyncio
async def test_export_csv_writes_rows(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HOME", str(tmp_path))
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        editor = app._tabs().current_editor
        assert editor is not None
        editor.text = "SELECT 1 AS a, 'hi' AS b UNION ALL SELECT 2, 'bye'"
        await app.action_run_query()
        # ``action_run_query`` now starts a worker and returns, so the pump
        # stays free for the cancel key. Wait on the worker rather than
        # hoping a single ``pause()`` outlasts the query.
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.action_export_csv()
        exports = list((tmp_path / ".registro" / "exports").glob("export-*.csv"))
        assert len(exports) == 1
        with exports[0].open() as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["a", "b"]
        assert rows[1:] == [["1", "hi"], ["2", "bye"]]


@pytest.mark.asyncio
async def test_query_with_duplicate_columns() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        editor = app._tabs().current_editor
        assert editor is not None
        editor.text = "SELECT 1 AS a, 2 AS a"
        await app.action_run_query()
        # ``action_run_query`` now starts a worker and returns, so the pump
        # stays free for the cancel key. Wait on the worker rather than
        # hoping a single ``pause()`` outlasts the query.
        await app.workers.wait_for_complete()
        await pilot.pause()
        results = app._results()
        assert results.loading is False
        assert results.row_count == 1
        cols = list(results.columns.values())
        assert len(cols) == 2
        assert str(cols[0].label) == "a"
        assert str(cols[1].label) == "a"


@pytest.mark.asyncio
async def test_run_summary_lands_on_the_bordered_panel() -> None:
    """The summary must be visible without the grid having focus.

    ``border_subtitle`` only renders on a widget that has a border, and
    ``ResultsTable`` has none except the one the ``:focus`` rule adds — so the
    row count was visible only while the grid happened to be focused. The
    bordered, titled container is its parent, ``#results-panel``.
    """
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        editor = app._tabs().current_editor
        assert editor is not None
        editor.text = "SELECT 1 AS a UNION ALL SELECT 2"
        await app.action_run_query()
        await app.workers.wait_for_complete()
        await pilot.pause()

        results = app._results()
        panel = app.query_one("#results-panel")
        assert app.focused is not results, "the grid must not be focused for this to mean anything"
        assert panel.styles.border.top[0], "results panel should have a border to write on"
        assert panel.border_subtitle == "2 rows"
        assert results.summary == "2 rows"


@pytest.mark.asyncio
async def test_a_new_run_clears_the_previous_summary() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        editor = app._tabs().current_editor
        assert editor is not None

        editor.text = "SELECT 1 AS a UNION ALL SELECT 2"
        await app.action_run_query()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#results-panel").border_subtitle == "2 rows"

        editor.text = "SELECT 1 WHERE 0"
        await app.action_run_query()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#results-panel").border_subtitle == "no rows returned"
