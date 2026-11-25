"""Generic storage router for worker uploads/downloads and storage-node handoff."""

import hashlib
import logging
import pathlib
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse

from relay_server.api.v2.security import get_alive_context
from relay_server.config import settings
from relay_server.core.artifacts import (
    ARTIFACT_LINK_DOWNLOADS_CAP,
    ARTIFACT_LINK_DOWNLOADS_DEFAULT,
    ARTIFACT_LINK_TTL_DEFAULT_SECONDS,
    ARTIFACT_LINK_TTL_MAX_SECONDS,
    ARTIFACT_LINK_TTL_MIN_SECONDS,
    _artifact_row_to_dict,
    create_artifact_link,
    get_artifact_metadata,
    list_artifacts,
    store_artifact_from_file,
)
from relay_server.core.chunked_upload import ChunkedUploadError, chunked_manager
from relay_server.core.db import get_conn, q
from relay_server.core.scheduler import Scheduler
from relay_server.models import (
    ArtifactLinkCreateRequest,
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
    ctx: AuthContext = Depends(get_alive_context),
):
    """Upload a standalone file and receive an artifact_id.

    Workers upload binary results here first, then reference the artifact_id
    in task payloads for storage nodes to archive onto long-term storage.
    T-005g: non-admin nodes may only attach artifacts to their own or
    ownerless tasks — a foreign-owned task is indistinguishable from a
    missing one (404).
    """
    owner_node_id = None if ctx.is_admin else ctx.node_id
    if owner_node_id is not None and task_id:
        task_owner = Scheduler.artifact_task_owner(task_id)
        if task_owner is not None and task_owner != owner_node_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        if task_owner is None:
            # artifact_task_owner returns None for missing AND ownerless
            # tasks — disambiguate before storing (FK requires the task row).
            conn = get_conn()
            try:
                exists = conn.execute(
                    q("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,))
                ).fetchone()
            finally:
                conn.close()
            if not exists:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
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
    ctx: AuthContext = Depends(get_alive_context),
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
    ctx: AuthContext = Depends(get_alive_context),
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


# ── T-033: signed short-lived download links ──────────────────────


def _link_error(code: int, detail: str) -> HTTPException:
    """404-style raise for the unauthenticated link path.

    The link GET is deliberately UNAUTHENTICATED — every failure mode
    answers 404 with a distinguishing detail, so probing a nonexistent
    token cannot distinguish 401/403 classes (no auth oracle).
    """
    return HTTPException(status_code=code, detail=detail)


@router.post("/files/{artifact_id}/link")
async def storage_file_link_create(
    artifact_id: str,
    data: ArtifactLinkCreateRequest,
    request: Request,
    ctx: AuthContext = Depends(get_alive_context),
):
    """Create a short-lived signed download link for an artifact (T-033).

    Same gate as the plain download (``get_alive_context``: any live token,
    no owner scope — the link never grants more than the creation right).
    Defaults: ttl 900 s, max_downloads 1; both clamp LOUD (422 on
    out-of-range, never silently softened). The plain URL token is sent
    back exactly once — only its SHA-256 hash is stored.
    """
    meta = get_artifact_metadata(artifact_id)
    if not meta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")

    ttl = (
        data.ttl_seconds
        if data.ttl_seconds is not None
        else ARTIFACT_LINK_TTL_DEFAULT_SECONDS
    )
    if not (ARTIFACT_LINK_TTL_MIN_SECONDS <= ttl <= ARTIFACT_LINK_TTL_MAX_SECONDS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"ttl_seconds out of range: {ttl} (accepted: "
                f"{ARTIFACT_LINK_TTL_MIN_SECONDS}..{ARTIFACT_LINK_TTL_MAX_SECONDS})"
            ),
        )
    max_downloads = (
        data.max_downloads
        if data.max_downloads is not None
        else ARTIFACT_LINK_DOWNLOADS_DEFAULT
    )
    if not (1 <= max_downloads <= ARTIFACT_LINK_DOWNLOADS_CAP):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"max_downloads out of range: {max_downloads} (accepted: "
                f"1..{ARTIFACT_LINK_DOWNLOADS_CAP})"
            ),
        )

    link = create_artifact_link(
        artifact_id,
        ttl_seconds=ttl,
        max_downloads=max_downloads,
        created_by="admin" if ctx.is_admin else ctx.node_id,
    )
    # The storage router is mounted under /relay/v2 — a shareable URL must
    # carry that prefix, otherwise every browser click 404s.
    base = str(request.base_url).rstrip("/")
    url = f"{base}/relay/v2/storage/files/{artifact_id}/dl/{link['token']}"
    return {
        "artifact_id": artifact_id,
        "url": url,
        "expires_at": datetime.fromtimestamp(link["expires_at"], tz=timezone.utc).isoformat(),
        "max_downloads": max_downloads,
        "ttl_seconds": ttl,
        "max_downloads_cap": ARTIFACT_LINK_DOWNLOADS_CAP,
    }


@router.get("/files/{artifact_id}/dl/{token}")
async def storage_file_link_download(
    artifact_id: str,
    token: str,
):
    """Consume a signed download link — NO auth dependency (T-033).

    The URL itself is the bearer token. Every failure mode below is 404
    (never 401/403): a dead link and a wrong token are indistinguishable
    classes, so there is no token-probing oracle on this path.
    """
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = get_conn()
    try:
        row = conn.execute(
            q(
                "SELECT link_id, expires_at, max_downloads FROM artifact_links"
                " WHERE artifact_id = ? AND token_hash = ?",
                (artifact_id, token_hash),
            ),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise _link_error(status.HTTP_404_NOT_FOUND, "link not found")
    if time.time() >= row["expires_at"]:
        raise _link_error(status.HTTP_404_NOT_FOUND, "link expired")

    meta = get_artifact_metadata(artifact_id)
    if not meta:
        raise _link_error(status.HTTP_404_NOT_FOUND, "Artifact not found")
    path = Path(meta["storage_path"])
    if not path.exists():
        raise _link_error(
            status.HTTP_404_NOT_FOUND, "Artifact file missing from storage"
        )

    # Atomic consume: one UPDATE with a guard clause. Two racing requests
    # can never both pass the guard, so max_downloads is exact even under
    # concurrency; the row survives exhaustion until the purge task.
    conn = get_conn()
    try:
        result = conn.execute(
            q(
                "UPDATE artifact_links SET downloads = downloads + 1"
                " WHERE link_id = ? AND downloads < max_downloads",
                (row["link_id"],),
            ),
        )
        conn.commit()
        consumed = result.rowcount or 0
    finally:
        conn.close()
    if consumed < 1:
        raise _link_error(status.HTTP_404_NOT_FOUND, "link exhausted")

    return FileResponse(
        path=path,
        filename=meta["name"],
        media_type=meta.get("mime_type") or "application/octet-stream",
    )


@router.delete("/files/{artifact_id}")
async def storage_file_delete(
    artifact_id: str,
    ctx: AuthContext = Depends(get_alive_context),
):
    """Delete an artifact by id.

    T-005g: non-admin nodes may only delete artifacts of their own or
    ownerless tasks — foreign-owned artifacts report as not found.
    """
    owner_node_id = None if ctx.is_admin else ctx.node_id
    ok = Scheduler.delete_artifact_scoped(artifact_id, owner_node_id=owner_node_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return {"status": "deleted", "artifact_id": artifact_id}


@router.get("/list")
async def storage_list(
    task_id: Optional[str] = Query(None),
    ctx: AuthContext = Depends(get_alive_context),
):
    """List stored artifacts, optionally filtered by task."""
    # T-005g: non-admin nodes only see artifacts of their own/ownerless tasks.
    owner_node_id = None if ctx.is_admin else ctx.node_id
    if owner_node_id is not None and task_id:
        return {"artifacts": Scheduler.list_artifacts_scoped(task_id, owner_node_id=owner_node_id), "viewer": ctx.node_id}
    if owner_node_id is not None:
        # Unfiltered listing: scope every task to this node (ownerless included).
        conn = get_conn()
        try:
            rows = conn.execute(
                q(
                    "SELECT a.* FROM artifacts a LEFT JOIN tasks t ON a.task_id = t.task_id"
                    " WHERE t.owner_node_id IS NULL OR t.owner_node_id = ?"
                    " ORDER BY a.created_at DESC",
                    (owner_node_id,),
                )
            ).fetchall()
        finally:
            conn.close()
        return {"artifacts": [_artifact_row_to_dict(r, include_storage_path=False) for r in rows], "viewer": ctx.node_id}
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
    ctx: AuthContext = Depends(get_alive_context),
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
    ctx: AuthContext = Depends(get_alive_context),
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
    ctx: AuthContext = Depends(get_alive_context),
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
