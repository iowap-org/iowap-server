"""Stub presence router for Phase 0."""

from fastapi import APIRouter, Depends

from relay_server.api.v2.security import get_auth_context
from relay_server.models import AuthContext

router = APIRouter()


@router.post("/update")
async def presence_update(ctx: AuthContext = Depends(get_auth_context)):
    return {"status": "not_implemented", "detail": "Phase 2", "node_id": ctx.node_id}


@router.get("/nodes")
async def presence_nodes(ctx: AuthContext = Depends(get_auth_context)):
    return {"nodes": [], "viewer": ctx.node_id}


@router.get("/{node_id}")
async def presence_node(node_id: str, ctx: AuthContext = Depends(get_auth_context)):
    return {"node_id": node_id, "status": "unknown", "viewer": ctx.node_id}
