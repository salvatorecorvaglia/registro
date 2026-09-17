"""Tests for RowLimitModal and dynamic row limit setting."""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from textual.widgets import Input, Static  # noqa: E402

from registro_core.config import RegistroConfig  # noqa: E402
from registro_core.models.connection import Dsn  # noqa: E402
from registro_tui.app import RegistroApp  # noqa: E402
from registro_tui.screens.row_limit import RowLimitModal  # noqa: E402


@pytest.mark.asyncio
async def test_row_limit_modal_lifecycle() -> None:
    app = RegistroApp(config=RegistroConfig(), initial_dsn=Dsn.parse("sqlite:///:memory:"))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()

        # Trigger action to set row limit
        await app.action_set_row_limit()
        await pilot.pause()

        modals = [s for s in app.screen_stack if isinstance(s, RowLimitModal)]
        assert modals, "row limit modal did not appear"

        # Set limit input value
        modal = modals[0]
        inp = modal.query_one("#limit-input", Input)
        inp.value = "456"

        # Submit
        await pilot.press("enter")
        await pilot.pause()

        assert not any(isinstance(s, RowLimitModal) for s in app.screen_stack)
        assert app.cfg.ui.max_display_rows == 456
        assert app._results().max_rows == 456


@pytest.mark.asyncio
async def test_row_limit_modal_reports_invalid_input_instead_of_ignoring_it() -> None:
    """Bad input must say so and keep the modal open, not look like a dead key."""
    app = RegistroApp(config=RegistroConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_set_row_limit()
        await pilot.pause()

        modal = next(s for s in app.screen_stack if isinstance(s, RowLimitModal))
        modal.query_one("#limit-input", Input).value = "not a number"

        await pilot.press("enter")
        await pilot.pause()

        assert any(isinstance(s, RowLimitModal) for s in app.screen_stack), (
            "invalid input dismissed the modal"
        )
        assert "whole number" in str(modal.query_one("#limit-error", Static).render())


@pytest.mark.asyncio
async def test_row_limit_modal_rejects_a_negative_limit() -> None:
    app = RegistroApp(config=RegistroConfig())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.action_set_row_limit()
        await pilot.pause()

        modal = next(s for s in app.screen_stack if isinstance(s, RowLimitModal))
        modal.query_one("#limit-input", Input).value = "-5"

        await pilot.press("enter")
        await pilot.pause()

        assert any(isinstance(s, RowLimitModal) for s in app.screen_stack)
        assert "negative" in str(modal.query_one("#limit-error", Static).render())
