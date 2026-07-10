"""In-memory event bus for SSE distribution."""

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, Iterable, Optional, Set


@dataclass
class _Subscriber:
    subscriber_id: str
    node_id: Optional[str]
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    event_types: Optional[Set[str]] = None
    dropped: int = field(default=0, init=False)
    _event_bus: Optional["EventBus"] = field(default=None, init=False)

    def put_nowait(self, event: dict) -> None:
        """Enqueue an event, dropping it and counting drops if the queue is full."""
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            # Warn when drops exceed threshold (fires once at exactly 100).
            if self.dropped == 100 and self._event_bus is not None:
                self._event_bus._publish_internal(
                    "subscriber_lagging",
                    {
                        "subscriber_id": self.subscriber_id,
                        "node_id": self.node_id,
                        "dropped": self.dropped,
                    },
                )


class EventBus:
    """Simple async event bus for cluster-wide SSE distribution.

    Subscribers may optionally filter by event type. Publishers never block:
    events are queued with ``put_nowait`` and dropped for slow consumers.
    ``publish_sync`` is safe to call from synchronous or threaded contexts.
    """

    DEFAULT_QUEUE_SIZE = 1000
    DEFAULT_HISTORY_SIZE = 500

    def __init__(
        self, queue_size: int = DEFAULT_QUEUE_SIZE, history_size: int = DEFAULT_HISTORY_SIZE
    ):
        self._queue_size = queue_size
        self._subscribers: Dict[str, _Subscriber] = {}
        self._history: list = []
        self._history_size = history_size

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def clear(self) -> None:
        """Remove all subscribers. Intended for test isolation."""
        self._subscribers.clear()
        self._history.clear()

    def recent(self, limit: int = 50) -> list:
        """Return recent published events (newest first)."""
        return list(reversed(self._history[-limit:]))

    def _generate_id(self, prefix: str = "sub") -> str:
        return f"{prefix}_{secrets.token_urlsafe(8)}"

    async def subscribe(
        self,
        node_id: Optional[str] = None,
        event_types: Optional[Iterable[str]] = None,
    ) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted events until the client disconnects.

        Each call receives a unique subscriber ID, so reconnects from the same
        node do not overwrite or silently drop events from other streams.
        """
        sid = self._generate_id()
        types_set: Optional[Set[str]] = set(event_types) if event_types else None
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        loop = asyncio.get_running_loop()
        sub = _Subscriber(sid, node_id, queue, loop, types_set)
        sub._event_bus = self
        self._subscribers[sid] = sub
        try:
            while True:
                event = await queue.get()
                current = self._subscribers.get(sid)
                dropped = current.dropped if current else 0
                yield _format_sse(event, dropped=dropped)
        except asyncio.CancelledError:
            pass
        finally:
            self._subscribers.pop(sid, None)

    async def publish(self, event_type: str, payload: dict) -> None:
        """Publish an event to matching subscribers.

        Awaitable but non-blocking: events are dropped for slow consumers
        rather than back-pressuring the caller.
        """
        event = _make_event(event_type, payload)
        self._history.append(event)
        if len(self._history) > self._history_size:
            self._history.pop(0)
        current_loop = asyncio.get_running_loop()
        for sub in list(self._subscribers.values()):
            if sub.event_types is not None and event_type not in sub.event_types:
                continue
            if sub.loop is current_loop:
                sub.put_nowait(event)
            else:
                sub.loop.call_soon_threadsafe(sub.put_nowait, event)

    def publish_sync(self, event_type: str, payload: dict) -> None:
        """Publish an event from a synchronous context without blocking."""
        event = _make_event(event_type, payload)
        self._history.append(event)
        if len(self._history) > self._history_size:
            self._history.pop(0)
        for sub in list(self._subscribers.values()):
            if sub.event_types is not None and event_type not in sub.event_types:
                continue
            try:
                sub.loop.call_soon_threadsafe(sub.put_nowait, event)
            except RuntimeError:
                # Subscriber's event loop is closed; count the drop.
                sub.dropped += 1

    def _publish_internal(self, event_type: str, payload: dict) -> None:
        """Publish an internal event without writing to history (avoids recursion)."""
        event = _make_event(event_type, payload)
        for sub in list(self._subscribers.values()):
            if sub.event_types is not None and event_type not in sub.event_types:
                continue
            try:
                sub.loop.call_soon_threadsafe(sub.put_nowait, event)
            except RuntimeError:
                pass


def _make_event(event_type: str, payload: dict) -> dict:
    return {
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def _format_sse(event: dict, dropped: int = 0) -> str:
    """Format an event as SSE with optional dropped count."""
    lines = [f"event: {event['type']}", f"data: {json.dumps(event)}"]
    if dropped > 0:
        lines.insert(1, f"X-Dropped: {dropped}")
    return "\n".join(lines) + "\n\n"


event_bus = EventBus()
