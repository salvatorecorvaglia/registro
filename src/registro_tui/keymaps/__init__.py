"""Keymap presets."""

from registro_tui.keymaps._labels import key_label
from registro_tui.keymaps.default import (
    ACTION_TO_KEY,
    DEFAULT_BINDINGS,
    GLOBAL_BINDINGS,
    RESULTS_BINDINGS,
)
from registro_tui.keymaps.vim import VIM_ACTION_TO_KEY, VIM_BINDINGS

__all__ = [
    "ACTION_TO_KEY",
    "DEFAULT_BINDINGS",
    "GLOBAL_BINDINGS",
    "RESULTS_BINDINGS",
    "VIM_ACTION_TO_KEY",
    "VIM_BINDINGS",
    "key_label",
]
