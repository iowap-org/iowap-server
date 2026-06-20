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
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    event_types: Optional[Set[str]] = None
    dropped: int = field(default=0, init=False)


class EventBus:
    """Simple async event bus for cluster-wide SSE distribution.

    Subscribers may optionally filter by event type. Publishers never block:
    events are queued with ``put_nowait`` and dropped for slow consumers.
    ``publish_sync`` is safe to call from synchronous or threaded contexts.
    """

    DEFAULT_QUEUE_SIZE = 1000

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE):
        self._queue_size = queue_size
        self._subscribers: Dict[str, _Subscriber] = {}

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def clear(self) -> None:
        """Remove all subscribers. Intended for test isolation."""
        self._subscribers.clear()

    def _generate_id(self, prefix: str = "sub") -> str:
        return f"{prefix}_{secrets.token_urlsafe(8)}"

    async def subscribe(
        self,
        subscriber_id: Optional[str] = None,
        event_types: Optional[Iterable[str]] = None,
    ) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted events until the client disconnects."""
        sid = subscriber_id or self._generate_id()
        types_set: Optional[Set[str]] = set(event_types) if event_types else None
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        loop = asyncio.get_running_loop()
        self._subscribers[sid] = _Subscriber(sid, queue, loop, types_set)
        try:
            while True:
                event = await queue.get()
                yield _format_sse(event)
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
        current_loop = asyncio.get_running_loop()
        for sub in list(self._subscribers.values()):
            if sub.event_types is not None and event_type not in sub.event_types:
                continue
            self._put_nowait(sub, event, current_loop)

    def publish_sync(self, event_type: str, payload: dict) -> None:
        """Publish an event from a synchronous context without blocking."""
        event = _make_event(event_type, payload)
        for sub in list(self._subscribers.values()):
            if sub.event_types is not None and event_type not in sub.event_types:
                continue
            try:
                sub.loop.call_soon_threadsafe(_put_nowait_callback, sub.queue, event)
            except RuntimeError:
                # Subscriber's event loop is closed; drop the event.
                sub.dropped += 1

    @staticmethod
    def _put_nowait(
        sub: _Subscriber,
        event: dict,
        current_loop: asyncio.AbstractEventLoop,
    ) -> None:
        try:
            if sub.loop is current_loop:
                sub.queue.put_nowait(event)
            else:
                sub.loop.call_soon_threadsafe(_put_nowait_callback, sub.queue, event)
        except asyncio.QueueFull:
            sub.dropped += 1


def _put_nowait_callback(queue: asyncio.Queue, event: dict) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        pass


def _make_event(event_type: str, payload: dict) -> dict:
    return {
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }


def _format_sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


event_bus = EventBus()
