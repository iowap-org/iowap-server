"""Generic SSE event stream for cluster events."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from relay_server.api.v2.security import get_auth_context
from relay_server.core.events import event_bus
from relay_server.models import AuthContext

router = APIRouter()

KNOWN_EVENT_TYPES = {
    "node_online",
    "node_offline",
    "task_created",
    "stage_claimed",
    "stage_completed",
    "presence_changed",
    "artifact_created",
}


@router.get("/stream")
async def events_stream(
    node: str = Query(..., description="Node ID subscribing to events"),
    types: Optional[list[str]] = Query(
        None,
        description="Event types to include. Repeat or comma-separate. Known types: "
        + ", ".join(sorted(KNOWN_EVENT_TYPES)),
    ),
    ctx: AuthContext = Depends(get_auth_context),
):
    """SSE stream for cluster events.

    The ``node`` query parameter must match the authenticated node. Clients may
    optionally filter the stream to one or more event types via ``types``.
    """
    if ctx.node_id != node:
        raise HTTPException(status_code=403, detail="Cannot subscribe for another node")

    event_types: Optional[set[str]] = None
    if types:
        # Flatten comma-separated values and repeated query params.
        flattened = [t.strip() for raw in types for t in raw.split(",") if t.strip()]
        event_types = set(flattened)
        unknown = event_types - KNOWN_EVENT_TYPES
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown event types: {', '.join(sorted(unknown))}",
            )

    return StreamingResponse(
        event_bus.subscribe(node_id=node, event_types=event_types),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
