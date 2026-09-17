"""Adapter registry — discovery by URL scheme."""

from __future__ import annotations

from typing import TYPE_CHECKING

from registro_core.errors import AdapterError

if TYPE_CHECKING:
    from registro_adapters.base import AbstractAdapter
    from registro_core.models.connection import Dsn


class AdapterRegistry:
    """Maps URL schemes (and adapter names) to adapter classes."""

    def __init__(self) -> None:
        self._by_scheme: dict[str, type[AbstractAdapter]] = {}
        self._by_name: dict[str, type[AbstractAdapter]] = {}

    def register(self, adapter_cls: type[AbstractAdapter]) -> None:
        self._by_name[adapter_cls.name] = adapter_cls
        for scheme in adapter_cls.schemes:
            self._by_scheme[scheme] = adapter_cls

    def for_scheme(self, scheme: str) -> type[AbstractAdapter]:
        try:
            return self._by_scheme[scheme]
        except KeyError as exc:
            raise AdapterError(
                f"no adapter registered for scheme {scheme!r}",
                context={"scheme": scheme, "known": sorted(self._by_scheme)},
            ) from exc

    def for_name(self, name: str) -> type[AbstractAdapter]:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise AdapterError(
                f"no adapter named {name!r}",
                context={"name": name, "known": sorted(self._by_name)},
            ) from exc

    def names(self) -> list[str]:
        return sorted(self._by_name)


def _build_default() -> AdapterRegistry:
    reg = AdapterRegistry()
    # Lazy imports so unused-driver modules don't crash on missing extras.
    # PLC0415 intentionally suppressed — each driver is an optional extra.
    try:
        from registro_adapters.duckdb_adapter import DuckDBAdapter  # noqa: PLC0415

        reg.register(DuckDBAdapter)
    except ImportError:
        pass
    try:
        from registro_adapters.sqlite_adapter import SQLiteAdapter  # noqa: PLC0415

        reg.register(SQLiteAdapter)
    except ImportError:
        pass
    try:
        from registro_adapters.postgres_adapter import PostgresAdapter  # noqa: PLC0415

        reg.register(PostgresAdapter)
    except ImportError:
        pass
    try:
        from registro_adapters.mysql_adapter import MySQLAdapter  # noqa: PLC0415

        reg.register(MySQLAdapter)
    except ImportError:
        pass
    return reg


_default: AdapterRegistry | None = None


def default_registry() -> AdapterRegistry:
    global _default  # noqa: PLW0603 - module-level cache for the process-wide registry
    if _default is None:
        _default = _build_default()
    return _default


def reset_default_registry() -> None:
    """Clear the process-wide registry cache.

    Intended for tests so each one starts from a freshly built registry
    instead of inheriting state through import order.
    """
    global _default  # noqa: PLW0603 - module-level cache for the process-wide registry
    _default = None


def create_adapter_for_dsn(dsn: Dsn, *, registry: AdapterRegistry | None = None) -> AbstractAdapter:
    reg = registry or default_registry()
    cls = reg.for_scheme(dsn.scheme)
    return cls()
