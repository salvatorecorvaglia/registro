"""Connection dialog — prompt the user for a DSN when none is provided."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import Button, Input, ListItem, ListView, Static

from registro_core.models.connection import Dsn
from registro_tui.screens._base import BaseModal

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Key


# Common DSN presets surfaced as clickable shortcuts.
_PRESETS: list[tuple[str, str]] = [
    ("DuckDB (in-memory)", "duckdb:///:memory:"),
    ("SQLite (in-memory)", "sqlite:///:memory:"),
    ("PostgreSQL (local)", "postgres://registro:registro@localhost:5432/registro"),
    ("MySQL (local)", "mysql://registro:registro@localhost:3306/registro"),
]


class ConnectionScreen(BaseModal[str]):
    """Modal prompting for a database DSN.

    Returns the raw DSN string on submit, or ``None`` on escape/cancel.
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="connection-modal"):
            yield Static("[b]Connect to Database[/]\n", id="conn-title")
            yield Input(
                placeholder="Enter DSN (e.g. duckdb:///:memory:)",
                id="conn-input",
            )
            with ListView(id="conn-presets"):
                for i, (label, dsn) in enumerate(_PRESETS):
                    yield ListItem(
                        Static(f"[bold]{label}[/]  [dim]{dsn}[/]"),
                        id=f"preset-{i}",
                    )
            yield Static("", id="conn-error", classes="modal-error")
            yield Button("Connect", id="conn-submit-btn", variant="primary")
            yield Static(
                "\n[dim](tab to select presets · click a preset to populate · "
                "enter to connect · escape to cancel)[/]",
                id="conn-hint",
            )

    def on_mount(self) -> None:
        self.query_one("#conn-input", Input).focus()

    def handles_enter_itself(self) -> bool:
        # A focused preset list emits ListView.Selected on enter.
        return self.query_one("#conn-presets", ListView).has_focus

    def on_key(self, event: Key) -> None:
        # Up/down move focus between the input and the preset list; everything
        # else (escape/enter) is the shared modal behaviour.
        if event.key == "down":
            inp = self.query_one("#conn-input", Input)
            if inp.has_focus:
                self.query_one("#conn-presets", ListView).focus()
        elif event.key == "up":
            lst = self.query_one("#conn-presets", ListView)
            if lst.has_focus and lst.index == 0:
                self.query_one("#conn-input", Input).focus()
        else:
            super().on_key(event)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item and event.item.id and event.item.id.startswith("preset-"):
            try:
                idx = int(event.item.id.removeprefix("preset-"))
                dsn = _PRESETS[idx][1]
            except (ValueError, IndexError):
                return
            inp = self.query_one("#conn-input", Input)
            inp.value = dsn
            self.submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "conn-submit-btn":
            self.submit()

    def _show_error(self, message: str) -> None:
        """Report a bad DSN in place, keeping the modal open.

        Returning silently made enter look like a dead key. Parsing here rather
        than after dismissal also means a typo is corrected in the field that
        holds it, instead of reopening the dialog and retyping the whole DSN.
        """
        # ``Static.update`` parses markup, and this renders exception text that
        # embeds the user's own DSN — typing ``[/]`` into the field would
        # otherwise raise MarkupError from inside the modal's render.
        self.query_one("#conn-error", Static).update(escape(message))

    def submit(self) -> None:
        value = self.query_one("#conn-input", Input).value.strip()
        if not value:
            self._show_error("Enter a DSN, or choose one of the presets below.")
            return
        try:
            Dsn.parse(value)
        except Exception as exc:
            # Any parse failure is user-facing: show it, do not dismiss.
            self._show_error(str(exc))
            return
        self.dismiss(value)
