from __future__ import annotations

import pytest

from registro_tui.app import _opens_transaction


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("", False),
        (" \n\t", False),
        ("BEGIN;", True),
        ("begin transaction", True),
        (" \nStArT\t\nTRANSACTION;", True),
        ("-- comment\n/* multiline\ncomment */ BEGIN", True),
        ("/**//**/START TRANSACTION", True),
        ("-- BEGIN", False),
        ("/* BEGIN */", False),
        ("/* unterminated BEGIN", False),
        ("/* outer /* inner */ BEGIN", True),
        ("BEGINNING", False),
        ("BEGIN_", False),
        ("START TRANSACTIONAL", False),
        ("START/* comment */TRANSACTION", False),
        ("SELECT 'BEGIN'", False),
        ("SELECT 1; BEGIN", False),
    ],
)
def test_opens_transaction(sql: str, expected: bool) -> None:
    assert _opens_transaction(sql) is expected


@pytest.mark.parametrize("suffix", ["SELECT 1", "BEGIN", ""])
def test_repeated_comment_delimiters(suffix: str) -> None:
    # This prefix caused exponential backtracking in the comment regex.
    sql = "/*" + "*//*" * 10_000 + suffix
    assert not _opens_transaction(sql)


def test_many_complete_leading_comments() -> None:
    assert _opens_transaction("/* comment */ \n" * 10_000 + "BEGIN")
