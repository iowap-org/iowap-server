"""T-207: SSE server heartbeat (keepalive) in the event stream.

Regression: the SSE stream only wrote bytes when events happened. A quiet
cluster sent nothing for hours. After a hard server kill (no EOF) the node
connection stayed a TCP zombie — the node read forever with ``timeout=None``
and never reconnected (observed across repeated server deploys).

Fix: the ``subscribe()`` generator emits an SSE comment (``: ping``) every
``sse_ping_interval_seconds`` (default 20) so the stream never goes fully
silent. SSE comments are ignored by client parsers; combined with the
node-side read timeout (T-186, iowap-node) this bounds reconnect detection
to ~60s instead of "indefinite".
"""

from __future__ import annotations

import asyncio

from relay_server.config import settings
from relay_server.core.events import EventBus


async def _collect_chunks(bus: EventBus, count: int) -> list[str]:
    """Collect up to ``count`` SSE chunks from ``bus.subscribe()``."""
    got: list[str] = []
    agen = bus.subscribe(node_id="test-node")
    try:
        async for chunk in agen:
            got.append(chunk)
            if len(got) >= count:
                break
    finally:
        await agen.aclose()
    return got


def test_t207_ping_emits_bytes_on_silent_bus(monkeypatch):
    """A silent bus still yields a ping comment per interval."""
    monkeypatch.setattr(settings, "sse_ping_interval_seconds", 0.1)
    bus = EventBus()
    chunks = asyncio.run(_collect_chunks(bus, 2))
    assert len(chunks) == 2
    assert all(": ping" in c for c in chunks)


def test_t207_ping_interval_from_settings(monkeypatch):
    """Interval changes with settings (configurable, not hardcoded)."""
    monkeypatch.setattr(settings, "sse_ping_interval_seconds", 0.05)
    bus = EventBus()
    chunks = asyncio.run(_collect_chunks(bus, 3))
    assert len(chunks) == 3


def test_t207_event_delivered_while_pinging(monkeypatch):
    """Real events are delivered promptly alongside the pings."""
    monkeypatch.setattr(settings, "sse_ping_interval_seconds", 0.05)
    bus = EventBus()
    got: list[str] = []
    agen = bus.subscribe(node_id="test-node")

    async def consume() -> None:
        try:
            async for chunk in agen:
                got.append(chunk)
                if len(got) >= 3:
                    break
        finally:
            await agen.aclose()

    async def drive() -> None:
        task = asyncio.create_task(consume())
        await asyncio.sleep(0.02)
        await bus.publish("task_created", {"task_id": "T-1"})
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    assert any("task_created" in c for c in got), got