"""Presence router for status updates and queries."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException

from relay_server.api.v2.security import get_auth_context
from relay_server.core.presence import get_presence, list_presence, update_presence
from relay_server.models import AuthContext

router = APIRouter()


@router.post("/update")
async def presence_update(
    body: Dict[str, Any],
    ctx: AuthContext = Depends(get_auth_context),
):
    """Update presence status for the authenticated node."""
    ok = update_presence(
        node_id=ctx.node_id,
        status=body.get("status"),
        mood=body.get("mood"),
        activity=body.get("activity"),
        progress=body.get("progress"),
        eta_seconds=body.get("eta_seconds"),
        next_available=body.get("next_available"),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Node not registered")
    return {"status": "ok", "node_id": ctx.node_id}


@router.get("/nodes")
async def presence_nodes(
    status: Optional[str] = None,
    ctx: AuthContext = Depends(get_auth_context),
):
    return {"presence": list_presence(status=status), "viewer": ctx.node_id}


@router.get("/{node_id}")
async def presence_node(
    node_id: str,
    ctx: AuthContext = Depends(get_auth_context),
):
    record = get_presence(node_id)
    if not record:
        raise HTTPException(status_code=404, detail="Presence not found")
    return {"presence": record, "viewer": ctx.node_id}
