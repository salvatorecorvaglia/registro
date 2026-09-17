"""Top-level pytest config."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from registro_adapters.registry import reset_default_registry

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate_adapter_registry() -> Iterator[None]:
    """Reset the process-wide adapter registry around every test.

    The registry is a module-level singleton; clearing it before and after
    each test stops state from leaking through import order.
    """
    reset_default_registry()
    yield
    reset_default_registry()
