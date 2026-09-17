"""Plugin discovery via entry points + manual registration."""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points
from typing import TYPE_CHECKING, Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from registro_core.errors import PluginError, PluginLoadError
from registro_core.events import EventBus, PluginLoaded, PluginUnloaded
from registro_core.logging import get_logger
from registro_core.protocols.plugin import PluginManifest
from registro_core.version import __version__ as _registro_version
from registro_plugins.context import PluginContext

if TYPE_CHECKING:
    from collections.abc import Iterable

    from registro_plugins.registry import PluginRegistry

logger = get_logger(__name__)


def version_satisfies(version: str, specifier: str) -> bool:
    """Check ``version`` against a PEP 440 ``specifier`` such as ``>=1.2,<2``.

    Delegates to :mod:`packaging` so the full grammar — including ``~=`` and
    pre-release handling — behaves the way every other Python tool does. A
    specifier we cannot parse is treated as *unsatisfied*: a plugin that
    declares a constraint we do not understand has not been shown to be
    compatible, and loading it anyway is how incompatible plugins crash the
    host.
    """
    spec = specifier.strip()
    if not spec:
        return True
    try:
        return Version(version) in SpecifierSet(spec, prereleases=True)
    except (InvalidSpecifier, InvalidVersion):
        logger.warning("plugin_specifier_invalid", specifier=specifier, version=version)
        return False


class PluginLoader:
    """Discovers and instantiates plugins.

    A plugin entry point points at a callable returning an object exposing::

        manifest: PluginManifest
        def on_load(ctx: PluginContext) -> None
        def on_unload(ctx: PluginContext) -> None
    """

    GROUP = "registro.plugins"

    def __init__(
        self,
        registry: PluginRegistry,
        event_bus: EventBus,
        *,
        enabled: Iterable[str] | None = None,
        disabled: Iterable[str] | None = None,
        services: dict[str, Any] | None = None,
    ) -> None:
        self._registry = registry
        self._bus = event_bus
        self._enabled = set(enabled or [])
        self._disabled = set(disabled or [])
        self._services = services or {}
        self._instances: dict[str, Any] = {}
        self._contexts: dict[str, PluginContext] = {}

    def _is_allowed(self, plugin_id: str) -> bool:
        if plugin_id in self._disabled:
            return False
        return not (self._enabled and plugin_id not in self._enabled)

    def discover(self) -> list[EntryPoint]:
        return list(entry_points(group=self.GROUP))

    def load_all(self) -> None:
        for ep in self.discover():
            try:
                self.load_entry_point(ep)
            except (PluginLoadError, PluginError):
                logger.exception("plugin_load_failed", entry_point=ep.name)

    def load_entry_point(self, ep: EntryPoint) -> None:
        try:
            factory = ep.load()
            plugin = factory()
        except Exception as exc:
            raise PluginLoadError(
                f"failed to instantiate plugin {ep.name!r}", context={"error": str(exc)}
            ) from exc
        self._register(plugin)

    def load_instance(self, plugin: Any) -> None:
        self._register(plugin)

    def _register(self, plugin: Any) -> None:
        try:
            manifest = getattr(plugin, "manifest", None)
        except Exception as exc:
            raise PluginLoadError(
                "plugin `.manifest` access raised an exception",
                context={"plugin_repr": repr(plugin), "error": str(exc)},
            ) from exc
        if not isinstance(manifest, PluginManifest):
            raise PluginLoadError(
                "plugin missing PluginManifest at `.manifest`",
                context={"plugin_repr": repr(plugin)},
            )
        if not self._is_allowed(manifest.id):
            logger.info("plugin_skipped", plugin_id=manifest.id)
            return
        if not version_satisfies(_registro_version, manifest.requires_registro):
            logger.warning(
                "plugin_incompatible",
                plugin_id=manifest.id,
                requires_registro=manifest.requires_registro,
                registro_version=_registro_version,
            )
            return
        self._registry.add_plugin(manifest)
        ctx = PluginContext(
            plugin_id=manifest.id,
            event_bus=self._bus,
            registry=self._registry,
            services=self._services,
        )
        self._contexts[manifest.id] = ctx
        on_load = getattr(plugin, "on_load", None)
        if callable(on_load):
            try:
                on_load(ctx)
            except Exception as exc:
                self._registry.remove_plugin(manifest.id)
                self._contexts.pop(manifest.id, None)
                # ``on_load`` may have subscribed before it failed; closing the
                # context tears those subscriptions down instead of leaving a
                # half-loaded plugin receiving events.
                ctx.close()
                raise PluginLoadError(
                    f"on_load failed for plugin {manifest.id!r}",
                    context={"error": str(exc)},
                ) from exc
        self._instances[manifest.id] = plugin
        self._bus.publish(PluginLoaded(plugin_id=manifest.id))
        logger.info("plugin_loaded", plugin_id=manifest.id, version=manifest.version)

    def context_for(self, plugin_id: str) -> PluginContext:
        """Return the long-lived context bound to a loaded plugin.

        Used by the host app to invoke ``Panel.factory(ctx)`` with the same
        context the plugin received in ``on_load`` — so panel widgets and
        hooks share state.
        """
        try:
            return self._contexts[plugin_id]
        except KeyError as exc:
            raise PluginError(
                f"plugin not loaded: {plugin_id!r}", context={"plugin_id": plugin_id}
            ) from exc

    def unload(self, plugin_id: str) -> None:
        plugin = self._instances.pop(plugin_id, None)
        if plugin is None:
            raise PluginError(f"plugin not loaded: {plugin_id!r}", context={"plugin_id": plugin_id})
        on_unload = getattr(plugin, "on_unload", None)
        ctx = self._contexts.pop(plugin_id, None)
        if callable(on_unload):
            if ctx is None:
                # No tracked context — the plugin was never fully loaded.
                # Skip on_unload rather than handing it a fabricated context
                # that shares none of the state it saw in on_load.
                logger.warning("plugin_unload_without_context", plugin_id=plugin_id)
            else:
                try:
                    on_unload(ctx)
                except Exception:
                    logger.exception("plugin_on_unload_failed", plugin_id=plugin_id)
        if ctx is not None:
            ctx.close()
        self._registry.remove_plugin(plugin_id)
        self._bus.publish(PluginUnloaded(plugin_id=plugin_id))

    def unload_all(self) -> None:
        """Unload all currently loaded plugins."""
        import contextlib  # noqa: PLC0415

        for plugin_id in list(self._instances.keys()):
            with contextlib.suppress(Exception):
                self.unload(plugin_id)
