"""Stub discovery router for Phase 0."""

from fastapi import APIRouter

router = APIRouter()


@router.post("/register")
async def discovery_register():
    return {"status": "not_implemented", "detail": "Phase 2"}


@router.post("/heartbeat")
async def discovery_heartbeat():
    return {"status": "not_implemented", "detail": "Phase 2"}


@router.get("/nodes")
async def discovery_nodes():
    return {"nodes": []}


@router.get("/query")
async def discovery_query(capability: str):
    return {"capability": capability, "nodes": []}
