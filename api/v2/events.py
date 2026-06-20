"""Generic SSE event stream for cluster events."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from relay_server.api.v2.security import get_auth_context
from relay_server.core.events import event_bus
from relay_server.models import AuthContext

router = APIRouter()


@router.get("/stream")
async def events_stream(
    node: str = Query(..., description="Node ID subscribing to events"),
    ctx: AuthContext = Depends(get_auth_context),
):
    """SSE stream for cluster events."""
    if ctx.node_id != node:
        raise HTTPException(status_code=403, detail="Cannot subscribe for another node")
    return StreamingResponse(
        event_bus.subscribe(node),
        media_type="text/event-stream",
    )
