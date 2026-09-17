from __future__ import annotations

import asyncio

import pytest

from registro_core.events import Event, EventBus, QueryCompleted, QueryStarted


@pytest.mark.asyncio
async def test_subscribe_receives_event() -> None:
    bus = EventBus()
    seen: list[QueryStarted] = []
    delivered = asyncio.Event()

    async def handler(ev: QueryStarted) -> None:
        seen.append(ev)
        delivered.set()

    bus.subscribe(QueryStarted, handler)
    bus.publish(QueryStarted(query_id="q", connection_id="c", sql_preview="select 1"))
    # Deterministic wait — no sleep-polling.
    await asyncio.wait_for(delivered.wait(), timeout=1)
    assert len(seen) == 1
    assert seen[0].query_id == "q"
    await bus.close()


@pytest.mark.asyncio
async def test_only_matching_type_delivered() -> None:
    bus = EventBus()
    other: list[object] = []

    async def handler(ev: QueryStarted) -> None:
        other.append(ev)

    bus.subscribe(QueryStarted, handler)
    # Publish a different event type — should not be delivered.
    bus.publish(QueryCompleted(query_id="x", rows=1, duration_ms=1.0))
    for _ in range(20):
        await asyncio.sleep(0)
    assert other == []
    await bus.close()


@pytest.mark.asyncio
async def test_slow_subscriber_does_not_block_fast_one() -> None:
    bus = EventBus()
    fast_seen: list[int] = []
    release = asyncio.Event()

    async def slow(ev: QueryStarted) -> None:
        await release.wait()

    async def fast(ev: QueryStarted) -> None:
        fast_seen.append(int(ev.query_id))

    bus.subscribe(QueryStarted, slow)
    bus.subscribe(QueryStarted, fast)
    for i in range(5):
        bus.publish(QueryStarted(query_id=str(i), connection_id="c", sql_preview=""))
    for _ in range(50):
        await asyncio.sleep(0)
        if len(fast_seen) == 5:
            break
    assert fast_seen == [0, 1, 2, 3, 4]
    release.set()
    await bus.close()


@pytest.mark.asyncio
async def test_overflow_drops_oldest_and_counts() -> None:
    bus = EventBus(queue_size=2)
    received: list[str] = []
    release = asyncio.Event()

    async def slow(ev: QueryStarted) -> None:
        await release.wait()
        received.append(ev.query_id)

    bus.subscribe(QueryStarted, slow)
    # First event is pulled off the queue by the drainer (and blocks on
    # ``release``); the queue itself can then hold 2 more before overflow.
    for i in range(6):
        bus.publish(QueryStarted(query_id=str(i), connection_id="c", sql_preview=""))
        await asyncio.sleep(0)
    assert bus.dropped_events >= 1
    release.set()
    for _ in range(100):
        await asyncio.sleep(0)
        if len(received) >= 3:
            break
    # The newest event must survive the drop-oldest policy.
    assert "5" in received
    await bus.close()


@pytest.mark.asyncio
async def test_overflow_preserves_terminal_event() -> None:
    bus = EventBus(queue_size=2)
    received: list[Event] = []
    release = asyncio.Event()

    async def slow(ev: Event) -> None:
        await release.wait()
        received.append(ev)

    bus.subscribe(Event, slow)
    # The drainer pulls the first event and blocks on ``release``; the queue
    # itself can then hold 2 before it overflows.
    bus.publish(QueryStarted(query_id="warmup", connection_id="c", sql_preview=""))
    await asyncio.sleep(0)
    bus.publish(QueryCompleted(query_id="done", rows=1, duration_ms=1.0))
    # Flood with non-terminal events — these should evict each other, never
    # the QueryCompleted already queued behind them.
    for i in range(10):
        bus.publish(QueryStarted(query_id=str(i), connection_id="c", sql_preview=""))
        await asyncio.sleep(0)
    assert bus.dropped_events >= 1
    release.set()
    for _ in range(200):
        await asyncio.sleep(0)
        if any(isinstance(e, QueryCompleted) for e in received):
            break
    assert any(isinstance(e, QueryCompleted) for e in received)
    await bus.close()


@pytest.mark.asyncio
async def test_per_subscribe_queue_size_override() -> None:
    bus = EventBus(queue_size=1024)
    release = asyncio.Event()

    async def slow(ev: QueryStarted) -> None:
        await release.wait()

    bus.subscribe(QueryStarted, slow, queue_size=1)
    for i in range(5):
        bus.publish(QueryStarted(query_id=str(i), connection_id="c", sql_preview=""))
        await asyncio.sleep(0)
    # With queue_size=1 (and one event in-flight in the drainer), overflow
    # should kick in well before the default 1024 would.
    assert bus.dropped_events >= 1
    release.set()
    await bus.close()


@pytest.mark.asyncio
async def test_publish_after_close_is_noop() -> None:
    bus = EventBus()
    seen: list[QueryStarted] = []

    async def handler(ev: QueryStarted) -> None:
        seen.append(ev)

    bus.subscribe(QueryStarted, handler)
    await bus.close()
    bus.publish(QueryStarted(query_id="q", connection_id="c", sql_preview=""))
    for _ in range(10):
        await asyncio.sleep(0)
    assert seen == []


@pytest.mark.asyncio
async def test_subscribe_after_close_raises() -> None:
    bus = EventBus()
    await bus.close()

    async def handler(ev: QueryStarted) -> None:
        return None

    with pytest.raises(RuntimeError, match="closed"):
        bus.subscribe(QueryStarted, handler)


@pytest.mark.asyncio
async def test_handler_exception_does_not_kill_subscription() -> None:
    bus = EventBus()
    received: list[str] = []
    calls = 0

    async def flaky(ev: QueryStarted) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("boom")
        received.append(ev.query_id)

    bus.subscribe(QueryStarted, flaky)
    bus.publish(QueryStarted(query_id="a", connection_id="c", sql_preview=""))
    bus.publish(QueryStarted(query_id="b", connection_id="c", sql_preview=""))
    for _ in range(50):
        await asyncio.sleep(0)
        if received:
            break
    assert received == ["b"]
    await bus.close()


@pytest.mark.asyncio
async def test_overflow_grows_queue_for_terminal_events() -> None:
    bus = EventBus(queue_size=2)
    received: list[Event] = []
    release = asyncio.Event()

    async def slow(ev: Event) -> None:
        await release.wait()
        received.append(ev)

    bus.subscribe(Event, slow)
    # The first event is pulled by the drainer and blocks on `release`
    bus.publish(QueryCompleted(query_id="in_flight", rows=0, duration_ms=0.0))
    await asyncio.sleep(0)

    # Fill the queue (max size 2) with terminal events
    bus.publish(QueryCompleted(query_id="t1", rows=0, duration_ms=0.0))
    bus.publish(QueryCompleted(query_id="t2", rows=0, duration_ms=0.0))
    await asyncio.sleep(0)

    # Publish another terminal event when the queue is already full of terminal events
    bus.publish(QueryCompleted(query_id="t3", rows=0, duration_ms=0.0))
    await asyncio.sleep(0)

    # No events should be reported as dropped
    assert bus.dropped_events == 0

    release.set()
    # Wait for all events to drain
    for _ in range(100):
        await asyncio.sleep(0)
        if len(received) >= 4:
            break

    # We should have received all 4 events, in order
    assert len(received) == 4
    assert all(isinstance(e, QueryCompleted) for e in received)
    query_ids = [e.query_id for e in received if isinstance(e, QueryCompleted)]
    assert query_ids == ["in_flight", "t1", "t2", "t3"]
    await bus.close()
