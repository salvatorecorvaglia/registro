"""Results viewer backed by a Textual ``DataTable``.

The grid holds at most :attr:`max_rows` rows (``0`` = unlimited); excess rows
from a large stream are dropped and flagged so a truncated view is never
mistaken for the full result set.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
from typing import TYPE_CHECKING, Any, ClassVar

from rich.markup import escape
from textual.binding import Binding
from textual.widgets import DataTable
from textual.widgets.data_table import CellDoesNotExist

from registro_core.export import csv_safe_row
from registro_tui.keymaps.default import RESULTS_BINDINGS

if TYPE_CHECKING:
    from pathlib import Path

    from registro_core.models.result import ResultBatch


# Rows inserted between yields to the event loop.
_CHUNK_ROWS = 1000


def _format_rows(rows: list[tuple[Any, ...]]) -> list[tuple[str, ...]]:
    """Render raw values for display.

    Cells are interpreted as Rich markup, so anything a database can return —
    JSON arrays, regexes, Python reprs — has to be escaped. A bare ``[/]``
    would otherwise raise ``MarkupError`` and take the render down with it.
    """
    return [tuple("[dim]NULL[/]" if v is None else escape(str(v)) for v in row) for row in rows]


class ResultsTable(DataTable[str]):
    DEFAULT_CSS = ""

    # Declared here rather than on the app so they cannot be shadowed by the
    # SQL editor. ``ctrl+e``/``ctrl+y`` are TextArea's line-end and redo; as
    # app-level bindings they never fired while the editor had focus. The
    # editor is not on this widget's focus chain, so here they are unambiguous —
    # and they are only meaningful when the grid is focused anyway. The actions
    # themselves live on the app and are reached by bubbling.
    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding(k, a, d) for (k, a, d) in RESULTS_BINDINGS
    ]

    def __init__(self) -> None:
        super().__init__(id="results-table", zebra_stripes=True, header_height=1)
        self.cursor_type = "cell"
        self.max_rows = 0  # 0 = unlimited; set from cfg.ui.max_display_rows
        self._initialized = False
        self._column_names: list[str] = []
        self._rows: list[tuple[Any, ...]] = []
        self._row_count = 0
        self._truncated = False
        self._summary = ""

    # Textual resolves a widget binding's action against the *declaring* node
    # only — ``_dispatch_action`` looks up ``action_<name>`` on that node and
    # does not bubble. These forwarders are what let the binding live here (out
    # of the SQL editor's reach) while the behaviour stays on the app, next to
    # the status-bar and notification handling it needs. Same ``run_action``
    # idiom as ``ActionBar.on_button_pressed``.

    async def action_copy_cell(self) -> None:
        await self.app.run_action("copy_cell")

    async def action_export_csv(self) -> None:
        await self.app.run_action("export_csv")

    def reset(self) -> None:
        self.clear(columns=True)
        self._initialized = False
        self._column_names = []
        self._rows = []
        self._row_count = 0
        self._truncated = False
        self._summary = ""
        self.border_subtitle = None

    async def feed(self, batch: ResultBatch) -> None:
        if not self._initialized and batch.columns:
            self._column_names = [c.name for c in batch.columns]
            for i, col in enumerate(batch.columns):
                self.add_column(escape(col.name), key=str(i))
            self._initialized = True
        if not batch.rows:
            return
        rows = batch.rows
        if self.max_rows:
            if self._row_count >= self.max_rows:
                self._truncated = True
                return
            remaining = self.max_rows - self._row_count
            if len(rows) > remaining:
                rows = rows[:remaining]
                self._truncated = True

        # Raw values are kept alongside the grid's own (escaped, Rich-rendered)
        # copy because copy-cell and CSV export must reproduce what the
        # database returned, not what was displayed.
        self._rows.extend(rows)

        # Format and insert in chunks rather than materializing the whole
        # batch first: peak memory stays at one chunk, and the await between
        # chunks keeps the UI responsive while a large batch lands.
        for start in range(0, len(rows), _CHUNK_ROWS):
            chunk = rows[start : start + _CHUNK_ROWS]
            self.add_rows(_format_rows(chunk))
            self._row_count += len(chunk)
            if start + _CHUNK_ROWS < len(rows):
                await asyncio.sleep(0)

    def mark_done(self, *, cancelled: bool = False) -> None:
        """Summarize the finished run.

        ``cancelled`` flags the result set as incomplete so a partial result
        is not mistaken for the full output.

        The text is published on :attr:`summary` as well as on this widget's
        own ``border_subtitle``. The grid has no border unless it happens to be
        focused, so the subtitle alone was invisible most of the time; the host
        mirrors :attr:`summary` onto ``#results-panel``, which is the bordered,
        titled container the user actually sees.
        """
        if not self._initialized or self._row_count == 0:
            self._summary = "cancelled — no rows" if cancelled else "no rows returned"
        else:
            n = self._row_count
            parts = [f"{n} row{'s' if n != 1 else ''}"]
            if self._truncated:
                parts.append(f"display capped at {self.max_rows}")
            if cancelled:
                parts.append("cancelled (partial)")
            self._summary = " · ".join(parts)
        self.border_subtitle = self._summary

    @property
    def summary(self) -> str:
        """One-line description of the last finished run, or ``""``."""
        return self._summary

    @property
    def truncated(self) -> bool:
        """True when the display capped the result set below its real size."""
        return self._truncated

    def current_cell_text(self) -> str | None:
        """Raw text of the cell under the cursor — never the escaped rendering."""
        if not self._initialized or self.row_count == 0:
            return None
        row, column = self.cursor_coordinate
        if 0 <= row < len(self._rows):
            source = self._rows[row]
            if 0 <= column < len(source):
                val = source[column]
                return "" if val is None else str(val)
        try:
            rendered = self.get_cell_at(self.cursor_coordinate)
        except CellDoesNotExist:
            return None
        return "" if rendered is None else str(rendered)

    async def export_csv(self, path: Path) -> int:
        columns = list(self._column_names)
        data = list(self._rows)

        def _write(p: Path, cols: list[str], data_rows: list[tuple[Any, ...]]) -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(Exception):
                p.parent.chmod(0o700)
            with p.open("w", newline="", encoding="utf-8") as f:
                with contextlib.suppress(Exception):
                    p.chmod(0o600)
                w = csv.writer(f)
                w.writerow(cols)
                # Defused so a value like ``=1+1`` is text in the spreadsheet
                # rather than a formula it evaluates on open.
                w.writerows(csv_safe_row(r) for r in data_rows)

        await asyncio.to_thread(_write, path, columns, data)
        return len(data)
