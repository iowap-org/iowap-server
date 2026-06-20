"""Stub discovery router for Phase 0."""

from fastapi import APIRouter, Depends

from relay_server.api.v2.security import get_auth_context
from relay_server.models import AuthContext

router = APIRouter()


@router.post("/register")
async def discovery_register(ctx: AuthContext = Depends(get_auth_context)):
    return {"status": "not_implemented", "detail": "Phase 2", "node_id": ctx.node_id}


@router.post("/heartbeat")
async def discovery_heartbeat(ctx: AuthContext = Depends(get_auth_context)):
    return {"status": "not_implemented", "detail": "Phase 2", "node_id": ctx.node_id}


@router.get("/nodes")
async def discovery_nodes(ctx: AuthContext = Depends(get_auth_context)):
    return {"nodes": [], "viewer": ctx.node_id}


@router.get("/query")
async def discovery_query(capability: str, ctx: AuthContext = Depends(get_auth_context)):
    return {"capability": capability, "nodes": [], "viewer": ctx.node_id}
