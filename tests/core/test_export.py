"""Spreadsheet-formula defusing for exported result sets."""

from __future__ import annotations

import pytest

from registro_core.export import csv_safe_cell, csv_safe_row


@pytest.mark.parametrize(
    "value",
    ["=1+1", "+1", "-cmd", "@SUM(A1)", "\tfoo", "\rbar"],
)
def test_formula_like_strings_are_quoted(value: str) -> None:
    """Excel, LibreOffice and Sheets all execute a cell that starts this way."""
    assert csv_safe_cell(value) == "'" + value


@pytest.mark.parametrize("value", ["plain", "a=b", "", "1+1"])
def test_ordinary_strings_are_untouched(value: str) -> None:
    assert csv_safe_cell(value) is value


def test_non_strings_keep_their_type() -> None:
    """A genuine negative number must stay a number, sign and all."""
    assert csv_safe_cell(-5) == -5
    assert csv_safe_cell(-5.5) == -5.5
    assert csv_safe_cell(None) is None
    assert csv_safe_cell(True) is True


def test_row_helper_applies_across_the_row() -> None:
    assert csv_safe_row(("=A1", 3, None, "ok")) == ("'=A1", 3, None, "ok")
