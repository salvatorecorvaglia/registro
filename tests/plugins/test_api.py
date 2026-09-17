"""Tests for plugin API dataclass validation and version constraint parsing."""

from __future__ import annotations

import pytest

from registro_plugins.api import Command, Panel
from registro_plugins.loader import version_satisfies


async def _noop(_ctx: object) -> None:
    return


def _panel(**overrides: object) -> Panel:
    defaults: dict[str, object] = {
        "id": "p",
        "title": "Panel",
        "placement": "left",
        "factory": lambda _ctx: None,
    }
    defaults.update(overrides)
    return Panel(**defaults)  # type: ignore[arg-type]


def test_command_requires_non_empty_id_and_title() -> None:
    Command(id="x", title="X", handler=_noop)  # valid
    with pytest.raises(ValueError, match="id"):
        Command(id="  ", title="X", handler=_noop)
    with pytest.raises(ValueError, match="title"):
        Command(id="x", title="", handler=_noop)


def test_panel_requires_non_empty_id_and_title() -> None:
    _panel()  # valid
    with pytest.raises(ValueError, match="id"):
        _panel(id="")
    with pytest.raises(ValueError, match="title"):
        _panel(title="   ")


def test_panel_rejects_bad_placement() -> None:
    with pytest.raises(ValueError, match="placement"):
        _panel(placement="middle")


def test_panel_rejects_non_positive_initial_size() -> None:
    with pytest.raises(ValueError, match="initial_size"):
        _panel(initial_size=0)
    with pytest.raises(ValueError, match="initial_size"):
        _panel(initial_size=-5)


@pytest.mark.parametrize(
    ("version", "specifier", "expected"),
    [
        ("0.1.0", ">=0.1.0", True),
        ("0.1.0", ">0.1.0", False),
        ("0.2.0", ">=0.1.0", True),
        ("0.1.0", ">=0.2.0", False),
        ("1.0.0", ">=0.1.0,<2.0.0", True),
        ("2.0.0", ">=0.1.0,<2.0.0", False),
        ("0.1.0", "==0.1.0", True),
        ("0.1.0", "!=0.1.0", False),
        ("0.1.0", "", True),
        # Compatible-release: ``~=0.1.0`` is ``>=0.1.0, ==0.1.*``.
        ("0.1.0", "~=0.1.0", True),
        ("0.1.5", "~=0.1.0", True),
        ("0.2.0", "~=0.1.0", False),
        # Suffixed versions must compare by PEP 440 rules, not by dropping the
        # non-numeric part: 1.0.0rc1 precedes 1.0.0.
        ("1.0.0rc1", ">=1.0.0", False),
        ("1.0.0", ">=1.0.0rc1", True),
        # A specifier we cannot parse means "compatibility unproven" — the
        # plugin is held back rather than loaded on a guess.
        ("1.0.0", "not-a-specifier", False),
        ("not-a-version", ">=1.0.0", False),
    ],
)
def test_version_satisfies(version: str, specifier: str, expected: bool) -> None:
    assert version_satisfies(version, specifier) is expected
