"""Default keymap. Bindings are ``(key, action, description)`` tuples.

Two scopes, because a key is only free if the *focused* widget does not want
it. Textual resolves bindings from the focused widget upward, so anything
declared app-wide loses to the SQL editor whenever the editor has focus — which
is the normal state while working.

``GLOBAL_BINDINGS`` must therefore avoid every key claimed by ``TextArea`` and
``Input``; ``tests/tui/test_keymap_conflicts.py`` enforces that against the
installed Textual rather than against a list copied into a comment here.

``RESULTS_BINDINGS`` are declared on :class:`~registro_tui.widgets.results_table.ResultsTable`
instead. They only fire when the results grid has focus — which is the only
time "copy this cell" or "export these rows" means anything — and the editor is
not on that focus chain, so they can keep the keys that read best.
"""

from __future__ import annotations

# App-level bindings. Every key here must be reachable while the SQL editor has
# focus, so none of them may collide with TextArea/Input.
GLOBAL_BINDINGS: list[tuple[str, str, str]] = [
    ("ctrl+enter", "run_query", "Run query"),
    ("ctrl+j", "run_query", "Run query"),
    # Not ``ctrl+c``: TextArea, Input, Screen and App all bind it to copy/quit,
    # and the focused widget wins — so the cancel key did nothing in the editor
    # while the help modal promised it aborted the query. ``b`` for "break".
    ("ctrl+b", "cancel_query", "Cancel query"),
    ("ctrl+p", "command_palette", "Command palette"),
    ("ctrl+t", "new_tab", "New tab"),
    # Not ``ctrl+w``: TextArea binds it to delete-word-left.
    ("ctrl+o", "close_tab", "Close tab"),
    ("ctrl+r", "refresh_schema", "Refresh schema"),
    # Not ``ctrl+h``: terminals send that for Backspace (it is ASCII BS), so an
    # app-level binding there fires on an ordinary edit keystroke.
    ("ctrl+g", "show_history", "Show history"),
    ("question_mark", "help", "Help"),
    ("ctrl+q", "quit", "Quit"),
]

# Results-grid bindings, declared on ResultsTable. ``ctrl+e`` and ``ctrl+y`` are
# TextArea's line-end and redo, which is exactly why these cannot be app-level —
# but the editor is never on the results grid's focus chain.
RESULTS_BINDINGS: list[tuple[str, str, str]] = [
    ("ctrl+y", "copy_cell", "Copy cell"),
    ("ctrl+e", "export_csv", "Export CSV"),
]

# Kept as the app-level list under its original name: ``RegistroApp.BINDINGS``,
# the vim keymap, and the help modal all mean "the global ones" by it.
DEFAULT_BINDINGS: list[tuple[str, str, str]] = GLOBAL_BINDINGS

# One key per action, so callers (e.g. ``BUILTIN_COMMANDS``) can look up a
# binding's display string instead of hand-duplicating it — a duplicated
# literal is what let the palette's hint strings drift from the real keymap.
# ``setdefault`` keeps the *first* key listed above for an action (e.g.
# ``run_query``'s primary binding is ``ctrl+enter``, not the ``ctrl+j`` alias).
# Both scopes feed it: the footer advertises the results-grid keys too.
ACTION_TO_KEY: dict[str, str] = {}
for _key, _action, _desc in (*GLOBAL_BINDINGS, *RESULTS_BINDINGS):
    ACTION_TO_KEY.setdefault(_action, _key)
del _key, _action, _desc
