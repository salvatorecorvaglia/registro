from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Theme:
    """A Registro theme rendered as Textual design tokens."""

    name: str
    background: str
    foreground: str
    surface: str
    primary: str
    accent: str
    success: str
    warning: str
    error: str
