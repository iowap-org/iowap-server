"""Generic storage router for worker uploads/downloads and storage-node handoff."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse

from relay_server.api.v2.security import get_approved_context
from relay_server.config import settings
from relay_server.core.artifacts import delete_artifact, get_artifact_metadata, list_artifacts, store_artifact
from relay_server.models import ArtifactReference, ArtifactUploadResponse, AuthContext

router = APIRouter()


@router.post("/upload", response_model=ArtifactUploadResponse)
async def storage_upload(
    file: UploadFile = File(...),
    task_id: Optional[str] = Query(None, description="Optional task to associate with"),
    stage_id: Optional[str] = Query(None, description="Optional stage to associate with"),
    ctx: AuthContext = Depends(get_approved_context),
):
    """Upload a standalone file and receive an artifact_id.

    Workers upload binary results here first, then reference the artifact_id
    in task payloads for storage nodes to archive onto long-term storage.
    """
    content_length = file.size
    if content_length is not None and content_length > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Upload exceeds maximum size of {settings.max_upload_bytes} bytes",
        )

    content = await file.read()
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Upload exceeds maximum size of {settings.max_upload_bytes} bytes",
        )

    result = store_artifact(
        name=file.filename or "unnamed",
        content=content,
        mime_type=file.content_type,
        task_id=task_id,
        stage_id=stage_id,
        created_by=ctx.node_id,
    )
    return ArtifactUploadResponse(**result)


@router.get("/files/{artifact_id}/meta")
async def storage_file_meta(
    artifact_id: str,
    ctx: AuthContext = Depends(get_approved_context),
):
    """Return artifact metadata without streaming the file."""
    meta = get_artifact_metadata(artifact_id)
    if not meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return ArtifactReference(
        artifact_id=meta["artifact_id"],
        name=meta["name"],
        mime_type=meta["mime_type"],
        size_bytes=meta["size_bytes"],
        created_by=meta["created_by"],
    )


@router.get("/files/{artifact_id}")
async def storage_file_download(
    artifact_id: str,
    ctx: AuthContext = Depends(get_approved_context),
):
    """Download an artifact by id."""
    meta = get_artifact_metadata(artifact_id)
    if not meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")

    from pathlib import Path

    path = Path(meta["storage_path"])
    if not path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Artifact file missing from storage",
        )

    return FileResponse(
        path=path,
        filename=meta["name"],
        media_type=meta.get("mime_type") or "application/octet-stream",
    )


@router.delete("/files/{artifact_id}")
async def storage_file_delete(
    artifact_id: str,
    ctx: AuthContext = Depends(get_approved_context),
):
    """Delete an artifact by id."""
    ok = delete_artifact(artifact_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return {"status": "deleted", "artifact_id": artifact_id}


@router.get("/list")
async def storage_list(
    task_id: Optional[str] = Query(None),
    ctx: AuthContext = Depends(get_approved_context),
):
    """List stored artifacts, optionally filtered by task."""
    return {"artifacts": list_artifacts(task_id=task_id), "viewer": ctx.node_id}
