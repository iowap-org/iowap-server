"""Stub scheduler router for Phase 0."""

from fastapi import APIRouter

router = APIRouter()


@router.post("/tasks")
async def scheduler_tasks():
    return {"status": "not_implemented", "detail": "Phase 3"}


@router.post("/claim")
async def scheduler_claim():
    return {"status": "not_implemented", "detail": "Phase 3"}


@router.post("/complete")
async def scheduler_complete():
    return {"status": "not_implemented", "detail": "Phase 3"}


@router.post("/fail")
async def scheduler_fail():
    return {"status": "not_implemented", "detail": "Phase 3"}
