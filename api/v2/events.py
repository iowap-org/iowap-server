"""Generic SSE event stream for cluster events."""

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from relay_server.core.events import event_bus

router = APIRouter()


@router.get("/stream")
async def events_stream(node: str = Query(..., description="Node ID subscribing to events")):
    """SSE stream for cluster events."""
    return StreamingResponse(
        event_bus.subscribe(node),
        media_type="text/event-stream",
    )
