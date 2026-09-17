"""Layered configuration: defaults → TOML file → env (`REGISTRO_*`) → CLI."""

from __future__ import annotations

import contextlib
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from registro_core.errors import ConfigError
from registro_core.logging import get_logger

logger = get_logger(__name__)


class _Section(BaseModel):
    """Base for the config sections.

    ``validate_assignment`` has to live on the section that owns the field:
    settings are mutated at runtime (the row-limit modal writes
    ``ui.max_display_rows``), and that assignment is validated by *UIConfig*,
    not by the enclosing ``RegistroConfig``. Setting it only on the parent left
    every nested write unchecked, so a value the schema would have rejected
    from a config file was accepted at runtime.
    """

    model_config = ConfigDict(validate_assignment=True)


class UIConfig(_Section):
    # ``Literal``, matching ``keymap``: an unknown theme name used to fall back
    # to monokai silently, so a typo looked like the theme setting being
    # ignored. Keep in step with ``registro_tui.themes.THEMES``, which
    # ``tests/core/test_config.py`` asserts.
    theme: Literal["monokai", "nord"] = "monokai"
    keymap: Literal["default", "vim"] = "default"
    row_batch_size: int = Field(default=1000, ge=10, le=100_000)
    # Max rows held in the results grid. ``0`` means unlimited; a positive
    # cap protects memory on very large result sets.
    max_display_rows: int = Field(default=10_000, ge=0)


class EngineConfig(_Section):
    default_timeout_seconds: float = Field(default=60.0, gt=0)
    max_history_entries: int = Field(default=1000, ge=10)
    pool_size: int = Field(default=5, ge=1, le=64)
    # Where the local query-history store lives. ``None`` uses
    # ``~/.registro/history.db``. Every other path in the app is configurable;
    # this one was hardcoded at the single call site.
    history_path: str | None = None


class LoggingConfig(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # ``json_lines`` — emit one JSON object per line (CI/Docker). ``None``
    # auto-detects based on whether stderr is a TTY.
    json_lines: bool | None = None


class PluginsConfig(_Section):
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)


class RegistroConfig(BaseModel):
    """Top-level Registro settings.

    Env vars: ``REGISTRO_<section>__<field>``, e.g. ``REGISTRO_UI__THEME=nord``.
    """

    model_config = ConfigDict(
        extra="ignore",
        frozen=False,
        validate_assignment=True,
    )

    ui: UIConfig = Field(default_factory=UIConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)


def default_config_path() -> Path:
    return Path.home() / ".config" / "registro" / "config.toml"


def _deep_merge(base: dict[str, Any], custom: dict[str, Any]) -> dict[str, Any]:
    res = dict(base)
    for k, v in custom.items():
        if isinstance(v, dict) and k in res and isinstance(res[k], dict):
            res[k] = _deep_merge(res[k], v)
        else:
            res[k] = v
    return res


def _get_env_config() -> dict[str, Any]:
    import json  # noqa: PLC0415
    import os  # noqa: PLC0415

    env_data: dict[str, Any] = {}
    # Sort so the result never depends on the OS/shell's environment
    # iteration order: a scalar (``REGISTRO_ENGINE``) and a nested field
    # (``REGISTRO_ENGINE__POOL_SIZE``) can both target the same top-level key,
    # and whichever is applied last wins. Sorting makes the shorter, less
    # specific key always precede the longer, more specific one, so a nested
    # override always and consistently wins over a same-section scalar.
    for key, val in sorted(os.environ.items()):
        if key.startswith("REGISTRO_"):
            name = key[len("REGISTRO_") :]
            if not name:
                continue
            parts = [p.lower() for p in name.split("__")]
            curr = env_data
            for part in parts[:-1]:
                if part in curr:
                    if not isinstance(curr[part], dict):
                        curr[part] = {}
                    curr = curr[part]
                else:
                    curr = curr.setdefault(part, {})
            # Try parsing values that look like JSON arrays/objects
            parsed_val: Any = val
            val_stripped = val.strip()
            if (val_stripped.startswith("[") and val_stripped.endswith("]")) or (
                val_stripped.startswith("{") and val_stripped.endswith("}")
            ):
                with contextlib.suppress(json.JSONDecodeError):
                    parsed_val = json.loads(val_stripped)
            curr[parts[-1]] = parsed_val
    return env_data


def load_config(path: Path | None = None, **overrides: Any) -> RegistroConfig:
    """Load configuration. File is optional; env/CLI overrides win."""
    toml_data: dict[str, Any] = {}
    cfg_path = path or default_config_path()
    if cfg_path.exists():
        # Warn, don't mutate: the config may hold DSN credentials, but silently
        # rewriting the permissions of a file we were only asked to *read* is a
        # surprise, and it fights with deliberate setups (a shared config on a
        # locked-down directory, a read-only mount, version-controlled dotfiles).
        with contextlib.suppress(OSError):
            if cfg_path.stat().st_mode & 0o077:
                logger.warning(
                    "config_permissions_permissive",
                    path=str(cfg_path),
                    hint="run `chmod 600` on it if it contains credentials",
                )
        try:
            with cfg_path.open("rb") as fh:
                toml_data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(
                f"failed to read config at {cfg_path}",
                context={"path": str(cfg_path), "error": str(exc)},
            ) from exc

    # Layer: TOML < Env Vars < CLI/Overrides
    data = _deep_merge(toml_data, _get_env_config())
    data = _deep_merge(data, overrides)
    try:
        return RegistroConfig.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(
            "invalid configuration",
            context={"path": str(cfg_path), "error": str(exc)},
        ) from exc
