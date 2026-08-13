"""Discovery router with heartbeat, node list, capability query, and capability list/detail."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from relay_server.api.v2.security import get_auth_context
from relay_server.config import settings
from relay_server.core.discovery import (
    get_capabilities,
    get_capability_by_name,
    heartbeat,
    list_nodes,
    query_nodes_by_capability,
)
from relay_server.models import (
    AuthContext,
    DiscoveryDetailResponse,
    DiscoveryResponse,
    HeartbeatRequest,
    NodeHeartbeatRequest,
)

router = APIRouter()


@router.post("/heartbeat")
async def discovery_heartbeat(
    body: HeartbeatRequest,
    ctx: AuthContext = Depends(get_auth_context),
):
    """Node heartbeat updating last_seen and optional metadata.

    T-081: an optional ``status`` field lets the node request an
    explicit status transition (e.g. ``busy`` / ``idle`` from
    ``node-cli node busy``/``idle``). ``load_cap`` carries the
    per-node load ceiling used by the auto-busy logic.
    """
    ok = heartbeat(
        node_id=ctx.node_id,
        load=body.load,
        queue_depth=body.queue_depth,
        available=body.available,
        endpoint=body.endpoint,
        capabilities=[c.model_dump() for c in body.capabilities] if body.capabilities else None,
        node_name=body.node_name,
        description=body.description,
        routes=[r.model_dump() for r in body.routes] if body.routes else None,
        status=body.status,
        load_cap=body.load_cap,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Node not registered")
    return {"status": "ok", "node_id": ctx.node_id}


@router.post("/worker-heartbeat")
async def discovery_worker_heartbeat(
    body: NodeHeartbeatRequest,
    ctx: AuthContext = Depends(get_auth_context),
):
    """Worker heartbeat with full capability data (replace mode).

    T-081: forwards ``status`` and ``load_cap`` like the regular
    heartbeat endpoint.
    """
    ok = heartbeat(
        node_id=ctx.node_id,
        load=body.load,
        queue_depth=body.queue_depth,
        available=body.available,
        endpoint=body.endpoint,
        capabilities=body.capabilities,
        replace_capabilities=True,
        node_name=body.node_name,
        description=body.description,
        routes=body.routes,
        status=body.status,
        load_cap=body.load_cap,
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
    """Detail of a single capability including input schema and offering nodes."""
    cap = get_capability_by_name(name)
    if not cap:
        raise HTTPException(status_code=404, detail=f"Capability '{name}' not found")
    return cap


@router.get("/transfer-config")
async def get_transfer_config(
    ctx: AuthContext = Depends(get_auth_context),
):
    """T-164: Datei-Übertragungs-Treppe (Server-Konfig, node-cli liest sie).

    Liefert die Schwellen, anhand derer die node-cli entscheidet, ob eine
    Datei inline (base64 im Task-Payload), als Artifact (transienter
    Relay-Store) oder per Bridge (direkt zum Storage-Node) übertragen
    wird. ``max_payload_bytes`` wird mitgeliefert, damit die node-cli den
    base64-Overhead prüfen kann.
    """
    return {
        "max_inline_bytes": settings.max_inline_bytes,
        "max_artifact_bytes": settings.max_artifact_bytes,
        "max_payload_bytes": settings.max_payload_bytes,
    }