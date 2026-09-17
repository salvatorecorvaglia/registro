"""Footer bar showing keyboard shortcuts and the current status.

Layout:  ^q Quit  ? Help  ^↵ Run Query  ^r Refresh  ...  Database Connected.

The bar is built with :meth:`Content.assemble` rather than from a markup
string. Two reasons, both learned the hard way:

* ``assemble`` takes ``(text, style)`` pairs and never parses the text, so a
  message can contain brackets — a SQL error quoting ``[col]``, a driver repr —
  without any escaping, and without the class of MarkupError this widget used
  to be one keystroke away from.
* The bar measures its own width to right-align the status. Measuring has to
  use the same parser that renders, and this widget renders through Textual,
  not rich — rich's parser rejects Textual's ``$variable`` styles outright.
"""

from __future__ import annotations

from typing import Any

from textual.content import Content
from textual.reactive import reactive
from textual.widgets import Static

from registro_tui.keymaps import ACTION_TO_KEY, VIM_ACTION_TO_KEY, key_label

# Actions surfaced in the footer, in display order, each with the short label
# the footer shows. Only the *label* is written here — the key is resolved from
# the active keymap, because a footer that restates its own keys is a footer
# that drifts from the real bindings the first time one of them moves.
# Labels stay hand-tuned rather than reusing the keymap descriptions: screen
# width is the constraint here, not completeness.
_DEFAULT_FOOTER_ACTIONS: list[tuple[str, str]] = [
    ("quit", "Quit"),
    ("help", "Help"),
    ("show_history", "History"),
    ("run_query", "Run Query"),
    ("cancel_query", "Cancel"),
    ("refresh_schema", "Refresh"),
    ("new_tab", "New Tab"),
    ("close_tab", "Close Tab"),
    ("copy_cell", "Copy"),
    ("export_csv", "Export"),
    ("command_palette", "Palette"),
]

_VIM_FOOTER_ACTIONS: list[tuple[str, str]] = [
    ("quit", "Quit"),
    ("help", "Help"),
    ("show_history", "History"),
    ("run_query", "Run Query"),
    ("cancel_query", "Cancel"),
    ("focus_editor_top", "Top"),
    ("focus_editor_bottom", "Bottom"),
    ("new_tab", "New Tab"),
    ("close_tab", "Close Tab"),
    ("copy_cell", "Copy"),
    ("export_csv", "Export"),
    ("command_palette", "Palette"),
]

# The bar's own background is ``$warning`` (a light gold), so severity cannot be
# carried by text colour: ``$warning`` text on it measures 1.00:1 — invisible —
# and ``$error`` only 2.2:1. Severity is drawn as an inverted chip instead:
# dark text on a severity-coloured block, which reads as a badge and clears
# 3:1 against its own background while staying distinguishable from the bar.
_SEVERITY_STYLES: dict[str, str] = {
    "error": "bold $background on $error",
    "warning": "bold $background on $warning-darken-3",
    "success": "bold $background on $success",
}


def _footer_bindings(actions: list[tuple[str, str]], keys: dict[str, str]) -> list[tuple[str, str]]:
    """Resolve ``(action, label)`` pairs against a keymap, skipping unbound ones."""
    return [(key_label(keys[action]), label) for action, label in actions if action in keys]


class StatusBar(Static):
    DEFAULT_CSS = ""

    connection: reactive[str] = reactive("disconnected")
    mode: reactive[str] = reactive("NORMAL")
    #: Plain text — never markup. Presentation belongs to :meth:`render`.
    message: reactive[str] = reactive("")
    #: One of ``""``, ``"error"``, ``"warning"``, ``"success"``.
    severity: reactive[str] = reactive("")
    rows: reactive[int] = reactive(0)
    duration_ms: reactive[float] = reactive(0.0)
    has_run: reactive[bool] = reactive(False)
    truncated: reactive[bool] = reactive(False)

    def __init__(self) -> None:
        super().__init__(id="status-bar")

    def validate_message(self, value: str | None) -> str:
        return value or ""

    def validate_severity(self, value: str | None) -> str:
        return value if value in _SEVERITY_STYLES else ""

    def notify_status(self, message: str, severity: str = "") -> None:
        """Set the status text and its severity in one step."""
        self.message = message
        self.severity = severity

    def _shortcuts(self, max_cells: int) -> Content:
        """Render as many shortcut hints as fit in *max_cells*.

        The hints are the *less* important half of the bar. They used to be
        rendered in full and the status right-aligned after them, so on any
        terminal narrower than about 140 columns the status — including every
        error message — was pushed off the right edge and simply not shown. An
        80-column terminal is ordinary, so that was most of them. Hints are
        dropped from the right until the status fits.
        """
        bindings = (
            _footer_bindings(_VIM_FOOTER_ACTIONS, VIM_ACTION_TO_KEY)
            if self.mode == "VIM"
            else _footer_bindings(_DEFAULT_FOOTER_ACTIONS, ACTION_TO_KEY)
        )
        parts: list[Any] = []
        used = 0
        if self.mode and self.mode != "NORMAL":
            badge = f" {self.mode} "
            if len(badge) + 2 <= max_cells:
                parts.extend(((badge, "bold reverse"), "  "))
                used += len(badge) + 2
        for index, (key, desc) in enumerate(bindings):
            gap = "  " if index else ""
            hint = f"{key} {desc}"
            if used + len(gap) + len(hint) > max_cells:
                break
            if gap:
                parts.append(gap)
            parts.extend(((key, "bold"), f" {desc}"))
            used += len(gap) + len(hint)
        return Content.assemble(*parts)

    def _status(self) -> Content:
        parts: list[Any] = []
        if self.truncated:
            parts.extend(((" display truncated ", _SEVERITY_STYLES["warning"]), "  ·  "))
        if self.message:
            style = _SEVERITY_STYLES.get(self.severity, "")
            parts.append((f" {self.message} ", style) if style else self.message)
        elif self.connection and self.connection != "disconnected":
            parts.append("Database Connected.")
        else:
            parts.append("Disconnected.")
        return Content.assemble(*parts)

    def render(self) -> Content:
        # Measured in terminal cells, not characters: the labels carry glyphs
        # like ``^↵`` whose rendered width is not their ``len()``.
        width = self.size.width or 80
        available = width - 2  # account for padding
        # The status wins the space. Whatever is left over goes to the hints,
        # minus a two-cell gap so they never touch.
        status = self._status()
        shortcuts = self._shortcuts(max(available - status.cell_length - 2, 0))
        padding_len = available - shortcuts.cell_length - status.cell_length
        separator = " " * padding_len if padding_len > 0 else "  "
        return Content.assemble(shortcuts, separator, status)
