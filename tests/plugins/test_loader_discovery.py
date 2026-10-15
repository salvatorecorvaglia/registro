"""Cover the real entry-point discovery path of ``PluginLoader``.

We don't install a real wheel — we monkeypatch
``registro_plugins.loader.entry_points`` to return a synthetic
``EntryPoint`` pointing at the in-tree fixture module.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from registro_core.errors import PluginLoadError
from registro_core.events import EventBus
from registro_plugins.loader import PluginLoader
from registro_plugins.registry import PluginRegistry


@pytest.mark.asyncio
async def test_discover_returns_entry_points_filtered_by_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = EntryPoint(
        name="sample",
        value="tests.fixtures.sample_plugin:factory",
        group=PluginLoader.GROUP,
    )

    def _fake_entry_points(*, group: str) -> tuple[EntryPoint, ...]:
        assert group == PluginLoader.GROUP
        return (fake,)

    monkeypatch.setattr("registro_plugins.loader.entry_points", _fake_entry_points)

    bus = EventBus()
    try:
        loader = PluginLoader(PluginRegistry(), bus)
        found = loader.discover()
        assert [ep.name for ep in found] == ["sample"]
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_load_all_drives_entry_point_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = EntryPoint(
        name="sample",
        value="tests.fixtures.sample_plugin:factory",
        group=PluginLoader.GROUP,
    )
    monkeypatch.setattr(
        "registro_plugins.loader.entry_points",
        lambda *, group: (fake,) if group == PluginLoader.GROUP else (),
    )

    bus = EventBus()
    registry = PluginRegistry()
    loader = PluginLoader(registry, bus)
    try:
        loader.load_all()
        cmds = registry.all_commands()
        assert any(c.id == "tests.sample.hi" for c in cmds)
        manifests = [m.id for m in registry.manifests()]
        assert "tests.sample" in manifests
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_load_entry_point_wraps_factory_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = EntryPoint(
        name="bad",
        value="tests.fixtures.sample_plugin:does_not_exist",
        group=PluginLoader.GROUP,
    )
    monkeypatch.setattr(
        "registro_plugins.loader.entry_points",
        lambda *, group: (bad,) if group == PluginLoader.GROUP else (),
    )

    bus = EventBus()
    try:
        loader = PluginLoader(PluginRegistry(), bus)
        with pytest.raises(PluginLoadError):
            loader.load_entry_point(bad)
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_a_disabled_entry_point_is_never_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: ``disabled`` was only checked after import + instantiation.

    The manifest id is not known until the plugin's code has run, so a
    "disabled" plugin still executed at every startup. Disabling by the
    entry-point name — the one ``registro plugins list`` prints — must stop it
    before import.
    """
    loaded: list[str] = []

    class _TrackingEntryPoint:
        name = "sample"
        value = "tests.fixtures.sample_plugin:factory"
        group = PluginLoader.GROUP

        def load(self) -> object:
            loaded.append(self.name)
            raise AssertionError("a disabled plugin was imported")

    bus = EventBus()
    registry = PluginRegistry()
    try:
        loader = PluginLoader(registry, bus, disabled=["sample"])
        loader.load_entry_point(_TrackingEntryPoint())  # type: ignore[arg-type]
        assert loaded == []
        assert list(registry.manifests()) == []
    finally:
        await bus.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [["tests.sample"], ["sample"]])
async def test_the_allowlist_accepts_the_manifest_id_or_entry_point_name(
    enabled: list[str],
) -> None:
    ep = EntryPoint(
        name="sample",
        value="tests.fixtures.sample_plugin:factory",
        group=PluginLoader.GROUP,
    )
    bus = EventBus()
    registry = PluginRegistry()
    try:
        PluginLoader(registry, bus, enabled=enabled).load_entry_point(ep)
        assert [m.id for m in registry.manifests()] == ["tests.sample"]
    finally:
        await bus.close()
