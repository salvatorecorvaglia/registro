"""SQL editor backed by Textual's TextArea with SQL syntax highlighting."""

from __future__ import annotations

from textual.widgets import TextArea


class SqlEditor(TextArea):
    DEFAULT_CSS = ""

    def __init__(self, initial: str = "", *, id: str | None = None) -> None:
        # One editor per tab, so the id comes from the tab: a shared fixed id
        # made ``#sql-editor`` match several widgets at once.
        super().__init__(initial, language="sql", id=id, show_line_numbers=True)
        self.tab_behavior = "indent"

    @property
    def sql(self) -> str:
        return str(self.text)
