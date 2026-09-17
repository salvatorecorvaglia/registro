from __future__ import annotations

from registro_core.errors import (
    AdapterConnectionError,
    AdapterError,
    ConfigError,
    PluginError,
    PluginLoadError,
    QueryCancelledError,
    QueryError,
    QueryTimeoutError,
    RegistroError,
)


def test_base_error_carries_message_and_context() -> None:
    err = RegistroError("boom", context={"key": "value"})
    assert err.message == "boom"
    assert err.context == {"key": "value"}
    assert str(err) == "[registro.unknown] boom :: {'key': 'value'}"


def test_base_error_without_context_omits_the_context_suffix() -> None:
    err = RegistroError("boom")
    assert err.context == {}
    assert str(err) == "[registro.unknown] boom"


def test_each_subclass_has_a_distinct_stable_code() -> None:
    assert ConfigError("x").code == "registro.config"
    assert AdapterError("x").code == "registro.adapter"
    assert AdapterConnectionError("x").code == "registro.adapter.connection"
    assert QueryError("x").code == "registro.query"
    assert QueryCancelledError("x").code == "registro.query.cancelled"
    assert QueryTimeoutError("x").code == "registro.query.timeout"
    assert PluginError("x").code == "registro.plugin"
    assert PluginLoadError("x").code == "registro.plugin.load"


def test_hierarchy_matches_the_documented_tree() -> None:
    assert issubclass(AdapterConnectionError, AdapterError)
    assert issubclass(QueryCancelledError, QueryError)
    assert issubclass(QueryTimeoutError, QueryError)
    assert issubclass(PluginLoadError, PluginError)
    for cls in (ConfigError, AdapterError, QueryError, PluginError):
        assert issubclass(cls, RegistroError)


def test_adapter_connection_error_does_not_shadow_the_builtin() -> None:
    # Deliberately not named ``ConnectionError`` — see the class docstring.
    assert not issubclass(AdapterConnectionError, ConnectionError)


def test_context_is_copied_not_aliased() -> None:
    ctx = {"a": 1}
    err = RegistroError("boom", context=ctx)
    ctx["a"] = 2
    assert err.context == {"a": 1}
