"""Action bar between Query Editor and Query Results.

Displays the results display cap and a Run Query button.

There is deliberately no transaction-mode indicator. One used to sit here
reading "Tx: Auto-Commit" permanently — it was never updated, because nothing
in the UI or the CLI reaches ``registro_engine.transaction``. A label that
cannot change is not a status, and it implied a feature that is not wired up.
The engine keeps its transaction support for plugins and future work.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from textual.reactive import reactive
from textual.widgets import Button, Static

if TYPE_CHECKING:
    from textual.app import ComposeResult


class ActionBar(Static):
    DEFAULT_CSS = ""

    limit: reactive[int] = reactive(500)
    connection_status: reactive[str] = reactive("Disconnected.")

    def __init__(self) -> None:
        super().__init__(id="action-bar")

    def compose(self) -> ComposeResult:
        yield Static(f"Display cap {self.limit}", classes="action-label", id="limit-label")
        yield Button("Run Query", id="run-query-btn", variant="primary")

    def watch_limit(self, value: int) -> None:
        with contextlib.suppress(Exception):
            limit_str = "Unlimited" if value == 0 else str(value)
            # "Limit" alone reads as a SQL LIMIT clause; this is the number of
            # rows the grid will hold, which is a different promise entirely.
            self.query_one("#limit-label", Static).update(f"Display cap {limit_str}")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-query-btn":
            # run_action dispatches by name, so this stays decoupled from the
            # concrete App subclass (App[Any] has no action_run_query attr).
            await self.app.run_action("run_query")
