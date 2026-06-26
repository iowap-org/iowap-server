"""Discovery router with heartbeat, node list, capability query, and capability list/detail."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from relay_server.api.v2.security import get_auth_context
from relay_server.core.discovery import (
    get_capabilities, get_capability_by_name,
    heartbeat, list_nodes, query_nodes_by_capability,
)
from relay_server.models import (
    AuthContext, DiscoveryDetailResponse, DiscoveryResponse,
    HeartbeatRequest, NodeHeartbeatRequest,
)

router = APIRouter()


@router.post("/heartbeat")
async def discovery_heartbeat(
    body: HeartbeatRequest,
    ctx: AuthContext = Depends(get_auth_context),
):
    """Node heartbeat updating last_seen and optional metadata."""
    ok = heartbeat(
        node_id=ctx.node_id,
        load=body.load,
        queue_depth=body.queue_depth,
        available=body.available,
        endpoint=body.endpoint,
        capabilities=[c.model_dump() for c in body.capabilities] if body.capabilities else None,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Node not registered")
    return {"status": "ok", "node_id": ctx.node_id}


@router.post("/worker-heartbeat")
async def discovery_worker_heartbeat(
    body: NodeHeartbeatRequest,
    ctx: AuthContext = Depends(get_auth_context),
):
    """Worker heartbeat mit vollständigen Capability-Daten (Replace-Mode)."""
    ok = heartbeat(
        node_id=ctx.node_id,
        load=body.load,
        queue_depth=body.queue_depth,
        available=body.available,
        endpoint=body.endpoint,
        capabilities=body.capabilities,
        replace_capabilities=True,
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


# ── Neue Endpoints: Capabilities ────────────────────────────────

@router.get("/capabilities", response_model=DiscoveryResponse)
async def list_capabilities(
    node_id: Optional[str] = Query(None, description="Filter by node"),
    type_filter: Optional[str] = Query(None, description="Filter by type (ai, tool, script, …)"),
    available: bool = Query(True, description="Only available nodes"),
    config_filter: Optional[str] = Query(
        None,
        description='Config filter as JSON, e.g. \'{"region": "eu-west"}\'',
    ),
    ctx: AuthContext = Depends(get_auth_context),
):
    """
    Liste aller Capabilities aller Nodes.

    Gruppiert nach Capability-Name, jeder Eintrag enthaelt die
    Nodes die diese Capability anbieten (mit load, queue_depth, config).
    """
    import json

    config_dict = None
    if config_filter:
        try:
            config_dict = json.loads(config_filter)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid config_filter JSON: {config_filter}",
            )

    caps = get_capabilities(
        capability_name=node_id,
        type_filter=type_filter,
        available_only=available,
        config_filter=config_dict,
    )
    return {"capabilities": caps}


@router.get("/capabilities/{name}", response_model=DiscoveryDetailResponse)
async def get_capability_detail(
    name: str,
    node_id: Optional[str] = Query(None, description="Filter by specific node"),
    ctx: AuthContext = Depends(get_auth_context),
):
    """Detail einer einzelnen Capability inkl. Input-Schema und anbietenden Nodes."""
    cap = get_capability_by_name(name)
    if not cap:
        raise HTTPException(status_code=404, detail=f"Capability '{name}' nicht gefunden")
    return cap