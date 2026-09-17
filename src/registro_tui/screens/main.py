"""Main screen — composes the Registro layout.

Layout:
    ┌── Data Catalog ──┐┌──── Query Editor ─────────────────────┐
    │ schema tree       ││  Tab 1  Tab 2                        │
    │                   ││  SQL editor                          │
    │                   │├──────────────────────────────────────┤
    │                   ││ Display cap 10000        [Run]  │
    │                   │├──── Query Results ───────────────────┤
    │                   ││  results data table                  │
    └──────────────────┘└──────────────────────────────────────┘

Plugin rails (#right-rail, #bottom-rail) are NOT yielded here. The
host app mounts them dynamically inside ``_mount_plugin_panels`` only
when at least one panel is registered for that placement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Container, Vertical

from registro_tui.widgets import QueryTabs, ResultsTable, SchemaTree, StatusBar
from registro_tui.widgets.action_bar import ActionBar

if TYPE_CHECKING:
    from textual.app import ComposeResult


class MainScreen(Container):
    DEFAULT_CSS = ""

    def compose(self) -> ComposeResult:
        with Container(id="main-grid"):
            with Vertical(id="sidebar"):
                yield SchemaTree()
            with Vertical(id="workspace"):
                with Vertical(id="editor-panel"):
                    yield QueryTabs()
                yield ActionBar()
                with Vertical(id="results-panel"):
                    yield ResultsTable()
        yield StatusBar()
