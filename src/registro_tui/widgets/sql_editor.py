"""SQL editor backed by Textual's TextArea with SQL syntax highlighting."""

from __future__ import annotations

from textual.widgets import TextArea


class SqlEditor(TextArea):
    DEFAULT_CSS = ""

    def __init__(self, initial: str = "") -> None:
        super().__init__(initial, language="sql", id="sql-editor", show_line_numbers=True)
        self.tab_behavior = "indent"

    @property
    def sql(self) -> str:
        return str(self.text)
