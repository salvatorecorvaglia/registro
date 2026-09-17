"""Query tabs widget — light wrapper over Textual's TabbedContent."""

from __future__ import annotations

from textual.widgets import TabbedContent, TabPane

from registro_tui.widgets.sql_editor import SqlEditor


class QueryTabs(TabbedContent):
    DEFAULT_CSS = ""

    def __init__(self) -> None:
        super().__init__(id="query-tabs")
        self._counter = 0

    def on_mount(self) -> None:
        self.new_tab()

    def new_tab(self, sql: str = "") -> None:
        self._counter += 1
        tab_id = f"tab-{self._counter}"
        pane = TabPane(f"query {self._counter}", SqlEditor(sql), id=tab_id)
        self.add_pane(pane)
        self.active = tab_id

    def close_current(self) -> None:
        current = self.active
        if not current:
            return
        # Always keep at least one tab so an editor is available; create a
        # fresh replacement before removing the last pane.
        if len(self.query(TabPane)) <= 1:
            self.new_tab()
        self.remove_pane(current)

    @property
    def current_editor(self) -> SqlEditor | None:
        active = self.active_pane
        if active is None:
            return None
        for child in active.query(SqlEditor):
            return child
        return None
