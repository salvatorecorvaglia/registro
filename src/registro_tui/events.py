"""TUI-internal events."""

from __future__ import annotations

from registro_core.events import Event


class RunActionRequested(Event):
    """Request the app to invoke ``action_<action>``."""

    action: str
