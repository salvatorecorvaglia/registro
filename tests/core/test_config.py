from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from registro_core.config import load_config
from registro_core.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path


def test_load_config_with_file(tmp_path: Path) -> None:
    cfg = tmp_path / "registro.toml"
    cfg.write_text(
        dedent(
            """
            [ui]
            theme = "nord"
            keymap = "vim"

            [engine]
            pool_size = 8
            """
        )
    )
    out = load_config(cfg)
    assert out.ui.theme == "nord"
    assert out.ui.keymap == "vim"
    assert out.engine.pool_size == 8


def test_load_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    out = load_config(tmp_path / "absent.toml")
    assert out.ui.theme == "monokai"
    assert out.engine.pool_size == 5


def test_load_config_deep_merges_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "registro.toml"
    cfg.write_text(
        dedent(
            """
            [ui]
            theme = "nord"
            keymap = "vim"
            """
        )
    )
    out = load_config(cfg, ui={"theme": "monokai"})
    assert out.ui.theme == "monokai"
    assert out.ui.keymap == "vim"


@pytest.mark.parametrize("set_scalar_first", [True, False])
def test_env_config_scalar_and_nested_conflict_is_order_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, set_scalar_first: bool
) -> None:
    """A scalar (``REGISTRO_ENGINE``) and a nested field (``REGISTRO_ENGINE__POOL_SIZE``)
    targeting the same section must resolve the same way regardless of which
    ``setenv`` call happens first — the merge must not depend on
    ``os.environ`` iteration order.
    """
    scalar = ("REGISTRO_ENGINE", '{"max_history_entries": 42}')
    nested = ("REGISTRO_ENGINE__POOL_SIZE", "8")
    ordered = [scalar, nested] if set_scalar_first else [nested, scalar]
    for name, value in ordered:
        monkeypatch.setenv(name, value)

    out = load_config(tmp_path / "absent.toml")

    assert out.engine.pool_size == 8


def test_unknown_theme_is_rejected() -> None:
    """A typo in ``ui.theme`` must fail loudly.

    ``theme`` was a bare ``str`` while ``keymap`` next to it was a ``Literal``,
    so an unrecognized name fell through to monokai at render time and looked
    like the setting being ignored.
    """
    with pytest.raises(ConfigError):
        load_config(None, ui={"theme": "dracula"})


def test_known_themes_are_accepted() -> None:
    # Imported locally: tests/core must not pull in Textual at module scope.
    from registro_tui.themes import THEMES  # noqa: PLC0415

    for name in THEMES:
        assert load_config(None, ui={"theme": name}).ui.theme == name


def test_theme_literal_matches_the_registered_themes() -> None:
    """The ``Literal`` and the theme registry must not drift apart."""
    import typing  # noqa: PLC0415

    from registro_core.config import UIConfig  # noqa: PLC0415
    from registro_tui.themes import THEMES  # noqa: PLC0415

    allowed = set(typing.get_args(UIConfig.model_fields["theme"].annotation))
    assert allowed == set(THEMES)


def test_settings_are_validated_on_assignment() -> None:
    """Runtime writes must honour the same constraints as the config file.

    The row-limit modal writes ``ui.max_display_rows`` directly; without
    ``validate_assignment`` those writes bypassed the field bounds entirely.
    """
    cfg = load_config(None)
    with pytest.raises(ValidationError):
        cfg.ui.max_display_rows = -1
    cfg.ui.max_display_rows = 42
    assert cfg.ui.max_display_rows == 42
