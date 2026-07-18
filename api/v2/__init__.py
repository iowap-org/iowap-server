"""API v2 routers — only core services."""

from fastapi import APIRouter

from relay_server.api.v2.admin import router as admin_router
from relay_server.api.v2.auth import router as auth_router
from relay_server.api.v2.capability_pages import router as capability_pages_router
from relay_server.api.v2.dashboard import router as dashboard_router
from relay_server.api.v2.discovery import router as discovery_router
from relay_server.api.v2.docs import router as docs_router
from relay_server.api.v2.events import router as events_router
from relay_server.api.v2.presence import router as presence_router
from relay_server.api.v2.scheduler import router as scheduler_router
from relay_server.api.v2.storage import router as storage_router

router = APIRouter()
router.include_router(docs_router, prefix="/docs", tags=["docs"])
router.include_router(auth_router, prefix="/auth", tags=["auth"])
router.include_router(admin_router, prefix="/admin", tags=["admin"])
router.include_router(discovery_router, prefix="/discovery", tags=["discovery"])
router.include_router(scheduler_router, prefix="/scheduler", tags=["scheduler"])
router.include_router(presence_router, prefix="/presence", tags=["presence"])
router.include_router(events_router, prefix="/events", tags=["events"])
router.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])
router.include_router(storage_router, prefix="/storage", tags=["storage"])
router.include_router(
    capability_pages_router, prefix="/capabilities", tags=["capability-pages"]
)
