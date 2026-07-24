"""Generic storage router for worker uploads/downloads and storage-node handoff."""

import logging
import pathlib
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse

from relay_server.api.v2.security import get_approved_context
from relay_server.config import settings
from relay_server.core.artifacts import (
    delete_artifact,
    get_artifact_metadata,
    list_artifacts,
    store_artifact_from_file,
)
from relay_server.core.chunked_upload import ChunkedUploadError, chunked_manager
from relay_server.models import (
    ArtifactReference,
    ArtifactUploadResponse,
    AuthContext,
    ChunkedChunkRequest,
    ChunkedCompleteRequest,
    ChunkedInitRequest,
)

logger = logging.getLogger(__name__)
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

    # Stream the incoming upload chunkwise into a SpooledTemporaryFile so we
    # never load the whole file into RAM. Rolls over to disk once it exceeds
    # the in-memory threshold (1 MiB).
    spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    total = 0
    try:
        while chunk := await file.read(64 * 1024):  # 64 KiB chunks
            total += len(chunk)
            if total > settings.max_upload_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"Upload exceeds maximum size of {settings.max_upload_bytes} bytes",
                )
            spool.write(chunk)
    except HTTPException:
        spool.close()
        raise
    except Exception:
        spool.close()
        raise

    spool.flush()
    spool.seek(0)

    # SpooledTemporaryFile only exposes a usable .name once it spilled to a
    # real on-disk temp file. Even then the default roll-over may report an
    # integer file descriptor instead of a path, so we only trust string-like
    # names. Whenever there's no usable path (RAM-only upload, or fd-only
    # roll-over), materialise the spooled bytes into a named temp file.
    real_path: Optional[pathlib.Path] = None
    spilled_name = getattr(spool, "name", None)
    if isinstance(spilled_name, (str, bytes, pathlib.PurePath)):
        candidate = pathlib.Path(spilled_name)
        if candidate.exists():
            real_path = candidate
    if real_path is None:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".upload") as out:
            while True:
                buf = spool.read(64 * 1024)
                if not buf:
                    break
                out.write(buf)
        real_path = pathlib.Path(out.name)
        logger.info("Materialised spooled upload to %s (no usable path from SpooledTemporaryFile)", real_path)

    # Close the spool immediately — we have the real_path now.
    spool.close()

    try:
        result = store_artifact_from_file(
            name=file.filename or "unnamed",
            file_path=real_path,
            mime_type=file.content_type,
            task_id=task_id,
            stage_id=stage_id,
            created_by=ctx.node_id,
        )
    finally:
        real_path.unlink(missing_ok=True)

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


# ── Chunked upload: init → chunk → complete ─────────────────────


def _chunked_error_status(exc: ChunkedUploadError) -> int:
    """Map a ChunkedUploadError to an HTTP status code."""
    msg = str(exc)
    if "not found" in msg:
        return status.HTTP_404_NOT_FOUND
    if "out of range" in msg or "must be" in msg or "is required" in msg:
        return status.HTTP_400_BAD_REQUEST
    return status.HTTP_400_BAD_REQUEST


@router.post("/chunked/init")
async def chunked_init(
    data: ChunkedInitRequest,
    ctx: AuthContext = Depends(get_approved_context),
):
    """Start a chunked-upload session and reserve an upload_id."""
    try:
        result = chunked_manager.init_upload(
            name=data.name,
            mime_type=data.mime_type,
            total_chunks=data.total_chunks,
            created_by=ctx.node_id,
        )
    except ChunkedUploadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return result


@router.post("/chunked/{upload_id}/chunk")
async def chunked_chunk(
    upload_id: str,
    data: ChunkedChunkRequest,
    ctx: AuthContext = Depends(get_approved_context),
):
    """Upload a single chunk for an existing upload session."""
    try:
        result = chunked_manager.store_chunk(
            upload_id=upload_id,
            chunk_index=data.chunk_index,
            data=data.data_b64,
        )
    except ChunkedUploadError as exc:
        raise HTTPException(status_code=_chunked_error_status(exc), detail=str(exc))
    return result


@router.post("/chunked/{upload_id}/complete")
async def chunked_complete(
    upload_id: str,
    data: ChunkedCompleteRequest,
    ctx: AuthContext = Depends(get_approved_context),
):
    """Assemble all chunks of an upload session into a single artifact."""
    try:
        file_path = chunked_manager.complete_upload(upload_id, checksum=data.checksum)
    except ChunkedUploadError as exc:
        raise HTTPException(status_code=_chunked_error_status(exc), detail=str(exc))

    session = chunked_manager.get_session(upload_id)
    if session is None:  # pragma: no cover - complete_upload just verified it
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Upload session not found")

    try:
        result = store_artifact_from_file(
            name=session["name"],
            file_path=file_path,
            mime_type=session.get("mime_type"),
            task_id=None,
            stage_id=None,
            created_by=session.get("created_by") or ctx.node_id,
        )
    finally:
        chunked_manager.discard_session(upload_id)

    return {
        "artifact_id": result["artifact_id"],
        "size_bytes": result["size_bytes"],
        "status": "created",
    }
