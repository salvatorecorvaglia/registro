"""Registro plugin API."""

from registro_plugins.api import Command, Panel, PanelPlacement, hook
from registro_plugins.context import PluginContext
from registro_plugins.loader import PluginLoader
from registro_plugins.registry import PluginRegistry

__all__ = [
    "Command",
    "Panel",
    "PanelPlacement",
    "PluginContext",
    "PluginLoader",
    "PluginRegistry",
    "hook",
]
