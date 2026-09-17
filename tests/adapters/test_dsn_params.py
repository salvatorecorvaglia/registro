"""DSN query parameters are allowlisted, not splatted into the driver."""

from __future__ import annotations

import pytest

from registro_adapters._params import coerce_params, coerce_value
from registro_adapters.sqlite_adapter import SQLiteAdapter
from registro_core.errors import AdapterConnectionError
from registro_core.models.connection import Dsn


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("false", False),
        ("42", 42),
        ("utf8mb4", "utf8mb4"),
        ("", ""),
    ],
)
def test_coerce_value(raw: str, expected: object) -> None:
    assert coerce_value(raw) == expected


def test_coerce_params_passes_allowed_keys_through() -> None:
    out = coerce_params({"uri": "true", "timeout": "30"}, {"uri", "timeout"}, adapter="sqlite")
    assert out == {"uri": True, "timeout": 30}


def test_coerce_params_names_the_offending_key() -> None:
    with pytest.raises(AdapterConnectionError) as excinfo:
        coerce_params({"uri": "true", "nope": "1"}, {"uri"}, adapter="sqlite")
    assert "nope" in str(excinfo.value)
    assert excinfo.value.context["unknown"] == ["nope"]


@pytest.mark.asyncio
async def test_unknown_dsn_param_fails_the_connection_clearly() -> None:
    """A typo must surface as our error, not a driver TypeError."""
    adapter = SQLiteAdapter()
    with pytest.raises(AdapterConnectionError, match="totally_bogus"):
        await adapter.connect(Dsn.parse("sqlite:///:memory:?totally_bogus=1"))


@pytest.mark.asyncio
async def test_allowed_dsn_param_still_reaches_the_driver() -> None:
    adapter = SQLiteAdapter()
    # ``uri=true`` is what makes the shared-cache in-memory form work at all.
    await adapter.connect(Dsn.parse("sqlite:///file:params-demo?mode=memory&cache=shared&uri=true"))
    try:
        assert await adapter.ping()
    finally:
        await adapter.close()
