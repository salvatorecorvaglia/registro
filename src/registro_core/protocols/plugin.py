"""Plugin protocol."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class PluginManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    version: str = "0.0.0"
    description: str = ""
    requires_registro: str = ">=0.1.0"
    tags: list[str] = Field(default_factory=list)


@runtime_checkable
class Plugin(Protocol):
    """The shape every plugin module exposes."""

    manifest: PluginManifest

    def on_load(self, ctx: object) -> None: ...
    def on_unload(self, ctx: object) -> None: ...
