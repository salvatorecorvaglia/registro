"""Helpers for writing result sets to files other tools will open."""

from __future__ import annotations

from typing import Any

# Leading characters that make a spreadsheet treat a cell as a formula. The tab
# and carriage return are here because Excel strips them and then re-examines
# the first surviving character.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe_cell(value: Any) -> Any:
    """Defuse a value that a spreadsheet would evaluate as a formula.

    A query can legitimately return ``=1+1`` or ``@SUM(A1)``, and Excel,
    LibreOffice and Sheets all *execute* a cell whose text starts that way —
    so an exported result set becomes a script that runs on open. Prefixing a
    single quote is the conventional defusing: the text is preserved and shown
    as typed, but the cell is no longer a formula.

    Only ``str`` values are touched, so a genuine negative number keeps its
    type and its sign.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def csv_safe_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """Apply :func:`csv_safe_cell` across a row."""
    return tuple(csv_safe_cell(v) for v in row)
