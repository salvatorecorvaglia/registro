"""Vim-flavored keymap. Editor itself runs in normal-mode style."""

from __future__ import annotations

from registro_tui.keymaps.default import DEFAULT_BINDINGS

VIM_BINDINGS: list[tuple[str, str, str]] = [
    *DEFAULT_BINDINGS,
    ("colon", "command_palette", "Ex command"),
    ("g,g", "focus_editor_top", "Top of editor"),
    ("shift+g", "focus_editor_bottom", "Bottom of editor"),
]

# Same contract as ``ACTION_TO_KEY`` in ``default.py``: first key listed for an
# action wins, so the shared defaults keep their primary binding and the vim
# additions only supply actions the default keymap has none for.
VIM_ACTION_TO_KEY: dict[str, str] = {}
for _key, _action, _desc in VIM_BINDINGS:
    VIM_ACTION_TO_KEY.setdefault(_action, _key)
del _key, _action, _desc
