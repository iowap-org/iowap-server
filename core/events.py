"""In-memory event bus for SSE distribution."""

import asyncio
import json
from typing import AsyncGenerator, Dict, Set


class EventBus:
    """Simple async event bus for cluster-wide SSE distribution."""

    def __init__(self):
        self._queues: Dict[str, asyncio.Queue] = {}
        self._subscribers: Set[str] = set()

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def subscribe(self, node_id: str) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted events until client disconnects."""
        queue: asyncio.Queue = asyncio.Queue()
        self._queues[node_id] = queue
        self._subscribers.add(node_id)
        try:
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            self._subscribers.discard(node_id)
            self._queues.pop(node_id, None)

    async def publish(self, event_type: str, payload: dict) -> None:
        """Publish event to all subscribers asynchronously."""
        event = {"type": event_type, "payload": payload}
        dead = []
        for node_id, queue in list(self._queues.items()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(node_id)
        for node_id in dead:
            self._queues.pop(node_id, None)
            self._subscribers.discard(node_id)

    def publish_sync(self, event_type: str, payload: dict) -> None:
        """Publish event from a synchronous context."""
        event = {"type": event_type, "payload": payload}
        dead = []
        for node_id, queue in list(self._queues.items()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(node_id)
        for node_id in dead:
            self._queues.pop(node_id, None)
            self._subscribers.discard(node_id)


event_bus = EventBus()
