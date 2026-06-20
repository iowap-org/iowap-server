"""Stub presence router for Phase 0."""

from fastapi import APIRouter

router = APIRouter()


@router.post("/update")
async def presence_update():
    return {"status": "not_implemented", "detail": "Phase 2"}


@router.get("/nodes")
async def presence_nodes():
    return {"nodes": []}


@router.get("/{node_id}")
async def presence_node(node_id: str):
    return {"node_id": node_id, "status": "unknown"}
