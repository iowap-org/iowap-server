"""Discovery router with heartbeat, node list, and capability query."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from relay_server.api.v2.security import get_auth_context
from relay_server.core.discovery import heartbeat, list_nodes, query_nodes_by_capability
from relay_server.models import AuthContext

router = APIRouter()


@router.post("/heartbeat")
async def discovery_heartbeat(
    body: dict,
    ctx: AuthContext = Depends(get_auth_context),
):
    """Node heartbeat updating last_seen and optional metadata."""
    ok = heartbeat(
        node_id=ctx.node_id,
        load=body.get("load"),
        queue_depth=body.get("queue_depth"),
        available=body.get("available"),
        endpoint=body.get("endpoint"),
        capabilities=body.get("capabilities"),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Node not registered")
    return {"status": "ok", "node_id": ctx.node_id}


@router.get("/nodes")
async def discovery_nodes(
    status: Optional[str] = Query(None),
    ctx: AuthContext = Depends(get_auth_context),
):
    return {"nodes": list_nodes(status=status), "viewer": ctx.node_id}


@router.get("/query")
async def discovery_query(
    capability: str = Query(..., description="Capability name to search for"),
    ctx: AuthContext = Depends(get_auth_context),
):
    return {
        "capability": capability,
        "nodes": query_nodes_by_capability(capability),
        "viewer": ctx.node_id,
    }
