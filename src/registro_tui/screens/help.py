"""Help modal — lists the currently active keybindings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.widgets import Static

from registro_tui.keymaps import GLOBAL_BINDINGS, RESULTS_BINDINGS, key_label
from registro_tui.keymaps.vim import VIM_BINDINGS
from registro_tui.screens._base import BaseModal

if TYPE_CHECKING:
    from textual.app import ComposeResult


def _rows(bindings: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """Collapse ``(key, action, description)`` triples into display rows.

    Several keys can drive one action (``run_query`` answers both ``ctrl+enter``
    and ``ctrl+j``), which used to render as two identical "Run query" lines.
    Aliases are joined onto one row instead, and every key goes through
    :func:`key_label` so the modal spells a binding the way the footer does —
    ``^↵``, not ``ctrl+enter``.
    """
    by_action: dict[str, tuple[list[str], str]] = {}
    for key, action, desc in bindings:
        keys, existing_desc = by_action.setdefault(action, ([], desc or action))
        label = key_label(key)
        if label not in keys:
            keys.append(label)
        by_action[action] = (keys, existing_desc)
    return [(" / ".join(keys), desc) for keys, desc in by_action.values()]


def _section(title: str, rows: list[tuple[str, str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(k) for k, _ in rows)
    return [
        f"[bold $warning]{title}[/]",
        *(f"  [bold $accent]{k.ljust(width)}[/]  {d}" for k, d in rows),
    ]


class HelpModal(BaseModal[None]):
    """Press ``?`` to view, ``escape`` or ``enter`` to dismiss.

    Uses :class:`BaseModal` like every other modal: help has nothing to submit,
    so ``enter`` simply closes it too.
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="help-modal"):
            yield Static("[b]Registro — Keybindings[/]\n", id="help-title")
            if self._is_vim():
                yield Static(
                    "[$warning]Note: Vim mode applies to global/panel navigation controls "
                    "(e.g., j/k in list views, G/gg in panels). The query editor itself "
                    "remains in standard insert mode.[/]\n",
                    id="help-vim-note",
                )
            # ``?`` is a printable character, so a focused editor or input
            # consumes it before the app sees it — correct Textual behaviour,
            # but worth saying out loud since the footer advertises the key.
            yield Static(
                "[$warning]Note: while the editor has focus, ``?`` types a question mark. "
                "Use the command palette to reach this help from there.[/]\n",
                id="help-focus-note",
            )
            yield Static(self._render_bindings(), id="help-bindings")
            yield Static("\n[dim](press escape to close)[/]", id="help-hint")

    def _is_vim(self) -> bool:
        cfg = getattr(self.app, "cfg", None)
        return cfg is not None and getattr(cfg.ui, "keymap", "default") == "vim"

    def _render_bindings(self) -> str:
        lines = _section("Global", _rows(GLOBAL_BINDINGS))

        results = _section("Results pane", _rows(RESULTS_BINDINGS))
        if results:
            # Called out separately because these only fire when the results
            # grid has focus — which is also why they can hold keys the SQL
            # editor would otherwise claim.
            lines.extend(["", *results])

        if self._is_vim():
            global_keys = {k for k, _, _ in GLOBAL_BINDINGS}
            extra = [b for b in VIM_BINDINGS if b[0] not in global_keys]
            vim = _section("Vim navigation", _rows(extra))
            if vim:
                lines.extend(["", *vim])

        if not lines:
            return "[dim]no bindings registered[/]"
        return "\n".join(lines)

    def submit(self) -> None:
        # Nothing to submit — enter is just another way to close the help.
        self.dismiss(None)
