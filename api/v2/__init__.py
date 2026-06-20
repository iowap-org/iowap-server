"""API v2 routers — only core services."""

from fastapi import APIRouter

from .auth import router as auth_router
from .discovery import router as discovery_router
from .events import router as events_router
from .presence import router as presence_router
from .scheduler import router as scheduler_router

router = APIRouter()
router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(discovery_router, prefix="/discovery", tags=["discovery"])
router.include_router(scheduler_router, prefix="/scheduler", tags=["scheduler"])
router.include_router(presence_router, prefix="/presence", tags=["presence"])
router.include_router(events_router, prefix="/events", tags=["events"])
