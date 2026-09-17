"""Tests for the engine transaction() context manager."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from registro_engine.transaction import transaction

if TYPE_CHECKING:
    from registro_core.protocols.adapter import DatabaseAdapter


class _FakeTx:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> _FakeTx:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self.committed = True
        else:
            self.rolled_back = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeAdapter:
    def __init__(self) -> None:
        self.tx = _FakeTx()

    async def begin(self) -> _FakeTx:
        return self.tx


@pytest.mark.asyncio
async def test_transaction_commits_on_success() -> None:
    adapter = _FakeAdapter()
    async with transaction(cast("DatabaseAdapter", adapter)) as active:
        assert active is adapter.tx
    assert adapter.tx.committed is True
    assert adapter.tx.rolled_back is False


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_exception() -> None:
    adapter = _FakeAdapter()
    with pytest.raises(RuntimeError, match="boom"):
        async with transaction(cast("DatabaseAdapter", adapter)):
            raise RuntimeError("boom")
    assert adapter.tx.rolled_back is True
    assert adapter.tx.committed is False
