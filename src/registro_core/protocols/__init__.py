"""Public protocols (structural interfaces) defining Registro extension points."""

from registro_core.protocols.adapter import DatabaseAdapter, Transaction
from registro_core.protocols.plugin import Plugin, PluginManifest

__all__ = [
    "DatabaseAdapter",
    "Plugin",
    "PluginManifest",
    "Transaction",
]
