"""The reported version must come from the installed distribution."""

from __future__ import annotations

import tomllib
from importlib.metadata import version
from pathlib import Path

from registro_core.version import DISTRIBUTION, __version__

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_distribution_name_matches_pyproject() -> None:
    with _PYPROJECT.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    assert project["name"] == DISTRIBUTION


def test_version_is_read_from_installed_metadata() -> None:
    """Regression: the lookup used the pre-rename name and always fell back.

    ``registro version`` and the plugin ``requires_registro`` check then both
    used a hard-coded string that would drift on the first release.
    """
    assert __version__ == version(DISTRIBUTION)
