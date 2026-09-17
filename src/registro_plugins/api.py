"""Public plugin API surface.

Plugins import only from here. Internal details (loader, registry) are
intentionally hidden so we can refactor without breaking plugins.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypeVar

if TYPE_CHECKING:
    from registro_plugins.context import PluginContext

PanelPlacement = Literal["left", "right", "bottom"]

F = TypeVar("F", bound=Callable[..., Any])

_HOOK_ATTR = "__registro_hook__"


def hook(event: str) -> Callable[[F], F]:
    """Tag a method with the name of the event it's meant to handle.

    This is a bookkeeping aid only: nothing in the loader auto-registers
    tagged methods. A plugin's ``on_load`` must still explicitly subscribe
    each handler via :meth:`PluginContext.subscribe` with the corresponding
    :class:`~registro_core.events.Event` subclass — ``hook``/:func:`is_hook`/
    :func:`hook_event` just let ``on_load`` discover its own tagged methods
    (e.g. by iterating ``dir(self)``) instead of listing them by hand.

    Example::

        @hook("query.started")
        async def warn_on_drop(self, ctx, ev: QueryStarted) -> None: ...


        # in on_load:
        for name in dir(self):
            fn = getattr(self, name)
            if is_hook(fn) and hook_event(fn) == "query.started":
                ctx.subscribe(QueryStarted, fn)
    """

    def _wrap(fn: F) -> F:
        setattr(fn, _HOOK_ATTR, event)
        return fn

    return _wrap


def is_hook(fn: object) -> bool:
    return hasattr(fn, _HOOK_ATTR)


def hook_event(fn: object) -> str:
    return str(getattr(fn, _HOOK_ATTR))


CommandHandler = Callable[["PluginContext"], Awaitable[None]]


_PLACEMENTS: frozenset[str] = frozenset(("left", "right", "bottom"))


@dataclass(frozen=True, slots=True)
class Command:
    """User-invocable action surfaced in the command palette."""

    id: str
    title: str
    handler: CommandHandler
    default_binding: str | None = None
    description: str = ""
    category: str = "Plugin"

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Command.id must be a non-empty string")
        if not self.title.strip():
            raise ValueError("Command.title must be a non-empty string")


@dataclass(frozen=True, slots=True)
class Panel:
    """A dockable panel contributed by a plugin.

    ``factory`` returns a Textual widget instance; we use ``Any`` here to
    keep this module dependency-free of Textual.
    """

    id: str
    title: str
    placement: PanelPlacement
    factory: Callable[[PluginContext], Any]
    initial_size: int = 30
    keys: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Panel.id must be a non-empty string")
        if not self.title.strip():
            raise ValueError("Panel.title must be a non-empty string")
        if self.placement not in _PLACEMENTS:
            raise ValueError(
                f"Panel.placement must be one of {sorted(_PLACEMENTS)}, got {self.placement!r}"
            )
        if self.initial_size <= 0:
            raise ValueError("Panel.initial_size must be a positive integer")
