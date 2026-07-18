"""Capability dashboard-page router.

Serves operator-supplied HTML pages stored under
``settings.capability_pages_dir / <name> / dashboard.html``. These pages
are embedded in the dashboard's "Capabilities" tab via an <iframe>.

The pages are uploaded by nodes through
``POST /relay/v2/storage/upload?capability=<name>`` (see storage.py) and
are stored separately from the artifact store — there is no DB entry,
the path is deterministically derived from the capability name.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from relay_server.config import settings

router = APIRouter()


def _safe_capability_segment(name: str) -> str:
    """Validate a capability name for use as a single path segment.

    Rejects empty, path separators, and dot-segment traversal so the name
    cannot escape ``capability_pages_dir``.
    """
    if not name or not isinstance(name, str):
        raise HTTPException(status_code=400, detail="capability name is required")
    if any(sep in name for sep in ("/", "\\")):
        raise HTTPException(
            status_code=400, detail="capability name must not contain path separators"
        )
    if name in (".", "..") or name.startswith("."):
        raise HTTPException(
            status_code=400, detail="capability name must not be a path-traversal segment"
        )
    return name


@router.get("/{name}/dashboard-page")
async def get_capability_dashboard_page(name: str):
    """Serve the dashboard HTML page for a capability."""
    safe_name = _safe_capability_segment(name)
    page_path = settings.capability_pages_dir / safe_name / "dashboard.html"
    if not page_path.is_file():
        raise HTTPException(
            status_code=404, detail="No dashboard page for this capability"
        )
    return FileResponse(
        str(page_path),
        media_type="text/html",
        headers={"X-Frame-Options": "SAMEORIGIN"},
    )