"""Tests for driver-native type-name inference."""

from __future__ import annotations

import datetime
import decimal
import uuid

import pytest

from registro_adapters._typeinfer import infer_column_type, infer_column_type_from_value
from registro_core.models.result import ColumnType


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ("integer", ColumnType.INTEGER),
        ("BIGINT", ColumnType.INTEGER),
        ("int4", ColumnType.INTEGER),
        ("serial", ColumnType.INTEGER),
        ("real", ColumnType.FLOAT),
        ("double precision", ColumnType.FLOAT),
        ("numeric", ColumnType.DECIMAL),
        ("decimal", ColumnType.DECIMAL),
        ("boolean", ColumnType.BOOL),
        ("date", ColumnType.DATE),
        ("time", ColumnType.TIME),
        ("timestamptz", ColumnType.DATETIME),
        ("datetime", ColumnType.DATETIME),
        ("text", ColumnType.STRING),
        ("varchar", ColumnType.STRING),
        ("jsonb", ColumnType.JSON),
        ("bytea", ColumnType.BINARY),
        ("uuid", ColumnType.UUID),
    ],
)
def test_infer_known_types(native: str, expected: ColumnType) -> None:
    assert infer_column_type(native) is expected


def test_infer_strips_parametric_suffix() -> None:
    assert infer_column_type("VARCHAR(255)") is ColumnType.STRING
    assert infer_column_type("numeric(10, 2)") is ColumnType.DECIMAL
    assert infer_column_type("char (10)") is ColumnType.STRING


def test_infer_is_case_and_whitespace_insensitive() -> None:
    assert infer_column_type("  Integer  ") is ColumnType.INTEGER


@pytest.mark.parametrize("native", [None, "", "   ", "geometry", "wibble"])
def test_infer_unknown_types(native: str | None) -> None:
    assert infer_column_type(native) is ColumnType.UNKNOWN


# ---- value-based inference ----
#
# SQLite's cursor description carries column *names* only — no types at all — so
# for that adapter the first row of data is the only typing evidence there is.
# The whole branch table was untested apart from int and str.


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # bool before int: bool *is* an int in Python, so order matters here.
        (True, ColumnType.BOOL),
        (False, ColumnType.BOOL),
        (0, ColumnType.INTEGER),
        (-17, ColumnType.INTEGER),
        (1.5, ColumnType.FLOAT),
        (decimal.Decimal("1.25"), ColumnType.DECIMAL),
        # datetime before date: datetime is a subclass of date.
        (datetime.datetime(2026, 1, 1, 12, 30), ColumnType.DATETIME),
        (datetime.date(2026, 1, 1), ColumnType.DATE),
        (datetime.time(12, 30), ColumnType.TIME),
        (uuid.UUID("12345678-1234-5678-1234-567812345678"), ColumnType.UUID),
        (b"bytes", ColumnType.BINARY),
        (bytearray(b"bytes"), ColumnType.BINARY),
        (memoryview(b"bytes"), ColumnType.BINARY),
        ("text", ColumnType.STRING),
        ({"k": "v"}, ColumnType.JSON),
        ([1, 2, 3], ColumnType.JSON),
        (object(), ColumnType.UNKNOWN),
        # A NULL in the first row says nothing about the column.
        (None, ColumnType.UNKNOWN),
    ],
)
def test_infer_column_type_from_value(value: object, expected: ColumnType) -> None:
    assert infer_column_type_from_value(value) == expected


def test_bool_is_not_reported_as_integer() -> None:
    """Regression guard for the ordering above.

    ``isinstance(True, int)`` is True, so an ``int`` check placed first would
    type every boolean column as an integer.
    """
    assert infer_column_type_from_value(True) is ColumnType.BOOL
    assert infer_column_type_from_value(1) is ColumnType.INTEGER


def test_datetime_is_not_reported_as_date() -> None:
    """``datetime`` subclasses ``date``; the specific check must come first."""
    assert infer_column_type_from_value(datetime.datetime(2026, 1, 1)) is ColumnType.DATETIME
    assert infer_column_type_from_value(datetime.date(2026, 1, 1)) is ColumnType.DATE
