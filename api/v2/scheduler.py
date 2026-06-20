"""Stub scheduler router for Phase 0."""

from fastapi import APIRouter, Depends

from relay_server.api.v2.security import get_approved_context
from relay_server.models import AuthContext

router = APIRouter()


@router.post("/tasks")
async def scheduler_tasks(ctx: AuthContext = Depends(get_approved_context)):
    return {"status": "not_implemented", "detail": "Phase 3", "node_id": ctx.node_id}


@router.post("/claim")
async def scheduler_claim(ctx: AuthContext = Depends(get_approved_context)):
    return {"status": "not_implemented", "detail": "Phase 3", "node_id": ctx.node_id}


@router.post("/complete")
async def scheduler_complete(ctx: AuthContext = Depends(get_approved_context)):
    return {"status": "not_implemented", "detail": "Phase 3", "node_id": ctx.node_id}


@router.post("/fail")
async def scheduler_fail(ctx: AuthContext = Depends(get_approved_context)):
    return {"status": "not_implemented", "detail": "Phase 3", "node_id": ctx.node_id}
