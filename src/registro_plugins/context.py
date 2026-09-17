"""PluginContext — the narrow runtime surface handed to each plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from registro_core.events import Event, EventBus
from registro_core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from registro_plugins.api import Command, Panel
    from registro_plugins.registry import PluginRegistry

logger = get_logger(__name__)
E = TypeVar("E", bound=Event)


class PluginContext:
    """Stable runtime API for plugins.

    Intentionally narrow: plugins should not reach into the DI container. We
    expose only what's needed to react to events and contribute UI/commands.
    """

    def __init__(
        self,
        *,
        plugin_id: str,
        event_bus: EventBus,
        registry: PluginRegistry,
        services: dict[str, Any] | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self._bus = event_bus
        self._registry = registry
        # Copy, don't alias: the loader hands the same mapping to every plugin,
        # and ``update_services`` would otherwise let one plugin rewrite what
        # every other plugin resolves.
        self._services = dict(services) if services else {}
        self.logger = logger.bind(plugin_id=plugin_id)
        self._subscriptions: list[tuple[type[Any], Callable[[Any], Awaitable[None]]]] = []

    def subscribe(self, event_type: type[E], handler: Callable[[E], Awaitable[None]]) -> None:
        self._bus.subscribe(event_type, handler)
        self._subscriptions.append((event_type, handler))

    def close(self) -> None:
        """Release any resources held by this context (e.g. event subscriptions)."""
        for event_type, handler in self._subscriptions:
            self._bus.unsubscribe(event_type, handler)
        self._subscriptions.clear()

    def publish(self, event: Event) -> None:
        self._bus.publish(event)

    def register_command(self, command: Command) -> None:
        self._registry.add_command(self.plugin_id, command)

    def register_panel(self, panel: Panel) -> None:
        self._registry.add_panel(self.plugin_id, panel)

    def get_service(self, name: str) -> Any:
        return self._services.get(name)

    def update_services(self, **services: Any) -> None:
        """Add (or replace) entries in this context's service bag.

        The host calls this *after* :meth:`on_load` once late-bound services
        (executor, introspector, history) become available on connect — so a
        plugin's hooks can resolve them via :meth:`get_service` without the
        host having to re-issue every context.
        """
        self._services.update(services)
