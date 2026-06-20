"""Stub auth router for Phase 0."""

from fastapi import APIRouter

router = APIRouter()


@router.post("/register")
async def auth_register():
    return {"status": "not_implemented", "detail": "Phase 1"}


@router.post("/refresh")
async def auth_refresh():
    return {"status": "not_implemented", "detail": "Phase 1"}
