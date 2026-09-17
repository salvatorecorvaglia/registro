"""Query history modal — pick a past query to load into the active tab."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import ListItem, ListView, Static

from registro_tui.screens._base import BaseModal

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from registro_engine.history import HistoryEntry


class HistoryModal(BaseModal["HistoryEntry"]):
    """Modal listing recent queries. Returns the selected entry, or ``None``."""

    def __init__(self, entries: list[HistoryEntry]) -> None:
        super().__init__()
        self._entries = entries

    def compose(self) -> ComposeResult:
        with Vertical(id="history-modal"):
            yield Static("[b]Query history[/]\n", id="history-title")
            yield ListView(id="history-list")
            yield Static("\n[dim](enter to load · escape to cancel)[/]", id="history-hint")

    def on_mount(self) -> None:
        lst = self.query_one("#history-list", ListView)
        if not self._entries:
            lst.append(ListItem(Static("[dim]no history yet[/]"), id="history-empty"))
            lst.focus()
            return
        for i, entry in enumerate(self._entries):
            preview_lines = entry.sql.strip().splitlines()
            preview = preview_lines[0] if preview_lines else ""
            if len(preview) > 80:
                preview = preview[:77] + "…"
            # Theme variables, so the dots track the active theme instead of
            # rendering as the terminal's own idea of green/red/yellow.
            status_style = {
                "succeeded": "$success",
                "failed": "$error",
                "cancelled": "$warning",
            }.get(entry.status, "$foreground")
            label = (
                f"[{status_style}]●[/] [bold]{escape(preview)}[/]\n"
                f"   [dim]{escape(entry.connection_label)}  ·  "
                f"{entry.duration_ms or 0:.1f} ms  ·  "
                f"{entry.rows or 0} rows[/]"
            )
            lst.append(ListItem(Static(label), id=f"history-{i}"))
        lst.focus()

    def handles_enter_itself(self) -> bool:
        # A focused ListView turns enter into ListView.Selected, which already
        # routes to submit(); handling it here too would submit twice.
        return self.query_one("#history-list", ListView).has_focus

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.submit()

    def submit(self) -> None:
        lst = self.query_one("#history-list", ListView)
        item = lst.highlighted_child
        if item is None or item.id is None or item.id == "history-empty":
            self.dismiss(None)
            return
        try:
            idx = int(item.id.removeprefix("history-"))
        except ValueError:
            self.dismiss(None)
            return
        if not 0 <= idx < len(self._entries):
            self.dismiss(None)
            return
        self.dismiss(self._entries[idx])
