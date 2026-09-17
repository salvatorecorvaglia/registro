"""Typed exception hierarchy for Registro.

Every error carries a stable, machine-readable ``code`` for telemetry and
plugin-side handling. Use ``code`` for branching, ``message`` for humans.
"""

from __future__ import annotations

from typing import Any


class RegistroError(Exception):
    """Base class for all Registro errors."""

    code: str = "registro.unknown"

    def __init__(self, message: str, /, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context) if context else {}

    def __str__(self) -> str:
        if self.context:
            return f"[{self.code}] {self.message} :: {self.context}"
        return f"[{self.code}] {self.message}"


class ConfigError(RegistroError):
    code = "registro.config"


class AdapterError(RegistroError):
    code = "registro.adapter"


class AdapterConnectionError(AdapterError):
    """Raised when a database adapter cannot establish or maintain a connection.

    .. note:: Deliberately not named ``ConnectionError`` to avoid shadowing
       Python's builtin :class:`ConnectionError` (a subclass of
       :class:`OSError`), which would silently catch the wrong exception type
       in modules that import from this package.
    """

    code = "registro.adapter.connection"


class QueryError(RegistroError):
    code = "registro.query"


class QueryCancelledError(QueryError):
    code = "registro.query.cancelled"


class QueryTimeoutError(QueryError):
    code = "registro.query.timeout"


class PluginError(RegistroError):
    code = "registro.plugin"


class PluginLoadError(PluginError):
    code = "registro.plugin.load"
