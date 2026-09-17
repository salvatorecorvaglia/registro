from __future__ import annotations

import pytest

from registro_adapters.registry import (
    create_adapter_for_dsn,
    default_registry,
    reset_default_registry,
)
from registro_adapters.sqlite_adapter import SQLiteAdapter
from registro_core.errors import AdapterError
from registro_core.models.connection import Dsn


def test_registry_has_known_adapters() -> None:
    reg = default_registry()
    names = reg.names()
    # sqlite is required (aiosqlite is a hard dep of engine); duckdb depends on extra
    assert "sqlite" in names


def test_registry_unknown_scheme_raises() -> None:
    reg = default_registry()
    with pytest.raises(AdapterError):
        reg.for_scheme("oracle")


def test_default_registry_is_cached_until_reset() -> None:
    first = default_registry()
    assert default_registry() is first
    reset_default_registry()
    assert default_registry() is not first


def test_for_name_returns_the_registered_adapter() -> None:
    assert default_registry().for_name("sqlite") is SQLiteAdapter


def test_for_name_reports_what_it_knows() -> None:
    with pytest.raises(AdapterError) as excinfo:
        default_registry().for_name("oracle")
    assert "oracle" in str(excinfo.value)
    assert "sqlite" in str(excinfo.value), "the error should list the known adapters"


def test_for_scheme_reports_what_it_knows() -> None:
    with pytest.raises(AdapterError) as excinfo:
        default_registry().for_scheme("oracle")
    assert excinfo.value.context["known"], "the error should carry the known schemes"


def test_create_adapter_for_dsn_builds_the_right_class() -> None:
    adapter = create_adapter_for_dsn(Dsn.parse("sqlite:///:memory:"))
    assert isinstance(adapter, SQLiteAdapter)
    assert adapter.connected is False, "construction must not connect"


def test_every_registered_adapter_declares_its_schemes() -> None:
    """A scheme-less adapter would register under no scheme and be unreachable."""
    registry = default_registry()
    for name in registry.names():
        cls = registry.for_name(name)
        assert cls.schemes, f"{name} registers no URL scheme"
        for scheme in cls.schemes:
            assert registry.for_scheme(scheme) is cls


def test_a_missing_driver_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional extras: an uninstalled driver must not break the registry.

    Each adapter import sits behind ``except ImportError``. Those four branches
    were never executed, so a genuinely broken import would have been
    indistinguishable from an absent optional dependency.
    """
    import builtins  # noqa: PLC0415

    real_import = builtins.__import__

    def _no_duckdb(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("registro_adapters.duckdb_adapter"):
            raise ImportError("duckdb not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", _no_duckdb)
    reset_default_registry()
    try:
        names = default_registry().names()
        assert "duckdb" not in names
        assert "sqlite" in names, "the other adapters must still register"
    finally:
        monkeypatch.undo()
        reset_default_registry()
