"""Fuzzy command palette (Ctrl+P)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rapidfuzz import fuzz, process
from rich.markup import escape
from textual.containers import Vertical
from textual.widgets import Input, ListItem, ListView, Static

from registro_plugins.api import Command
from registro_tui.screens._base import BaseModal

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Key


@dataclass(slots=True)
class _Entry:
    cmd: Command

    @property
    def label(self) -> str:
        # Titles/descriptions can come from third-party plugins — escape them
        # so a stray bracket cannot break the palette's rendering.
        binding = (
            f"  [dim]({escape(self.cmd.default_binding)})[/]" if self.cmd.default_binding else ""
        )
        subtitle = escape(self.cmd.description or self.cmd.id)
        return f"[b]{escape(self.cmd.title)}[/]{binding}\n[dim]{subtitle}[/]"


class CommandListItem(ListItem):
    def __init__(self, cmd: Command) -> None:
        super().__init__(Static(_Entry(cmd).label))
        self.cmd = cmd


class CommandPaletteScreen(BaseModal[Command]):
    DEFAULT_CSS = ""

    def __init__(self, commands: list[Command]) -> None:
        super().__init__()
        self._all = commands
        self._id_to_cmd = {c.id: c for c in commands}
        self._title_map = {c.id: f"{c.title} {c.description or ''}" for c in commands}

    def compose(self) -> ComposeResult:
        with Vertical(id="command-palette"):
            yield Static("[b]Command palette[/b]", id="palette-title")
            yield Input(placeholder="Search commands…", id="palette-input")
            yield ListView(id="palette-list")

    def on_mount(self) -> None:
        self._refresh("")
        self.query_one(Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh(event.value)

    def on_key(self, event: Key) -> None:
        # Arrow keys drive the result list while the search input keeps focus;
        # escape/enter fall through to the shared modal behaviour.
        if event.key in {"up", "down"}:
            self.query_one("#palette-list", ListView).post_message(event)
        else:
            super().on_key(event)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.submit()

    def _refresh(self, q: str) -> None:
        lst = self.query_one("#palette-list", ListView)
        lst.clear()
        ranked: list[Command]
        if not q:
            ranked = self._all
        else:
            matches = process.extract(
                q,
                self._title_map,
                scorer=fuzz.WRatio,
                limit=15,
            )
            ranked = [self._id_to_cmd[key] for (_choice, _score, key) in matches]
        for cmd in ranked[:15]:
            lst.append(CommandListItem(cmd))
        self.query_one("#palette-title", Static).update(
            f"[b]Command palette[/b]  [dim]{len(ranked)}/{len(self._all)}[/]"
        )

    def submit(self) -> None:
        lst = self.query_one("#palette-list", ListView)
        item = lst.highlighted_child
        if not isinstance(item, CommandListItem):
            self.dismiss(None)
            return
        self.dismiss(item.cmd)
