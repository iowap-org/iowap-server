"""Artifact storage core logic — keeps metadata in DB, files in artifacts_dir."""

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from relay_server.config import settings
from relay_server.core.db import get_conn
from relay_server.core.events import event_bus

logger = logging.getLogger("relay.artifacts")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(dt: datetime) -> str:
    return dt.isoformat()


def _generate_id(prefix: str = "id") -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _artifact_path(artifact_id: str) -> Path:
    # Simple shard: first 2 chars of id as subdir.
    shard = artifact_id.replace("artifact_", "")[:2]
    return settings.artifacts_dir / shard / artifact_id


def store_artifact(
    name: str,
    content: bytes,
    mime_type: Optional[str] = None,
    task_id: Optional[str] = None,
    stage_id: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Store an artifact file and its metadata."""
    artifact_id = _generate_id("artifact")
    now = _format_time(_now())
    path = _artifact_path(artifact_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    size = len(content)
    checksum = _sha256_file(path)

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO artifacts
            (artifact_id, task_id, stage_id, name, mime_type, size_bytes, checksum, storage_path, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                task_id,
                stage_id,
                name,
                mime_type,
                size,
                checksum,
                str(path),
                created_by,
                now,
            ),
        )
        conn.commit()
        event_bus.publish_sync(
            "artifact_created",
            {"artifact_id": artifact_id, "task_id": task_id, "created_by": created_by},
        )
        return {
            "artifact_id": artifact_id,
            "name": name,
            "path": str(path),
            "size_bytes": size,
            "mime_type": mime_type,
            "created_by": created_by,
        }
    finally:
        conn.close()


def store_artifact_from_file(
    name: str,
    file_path: Path,
    mime_type: Optional[str] = None,
    task_id: Optional[str] = None,
    stage_id: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Store an artifact by streaming a (temporary) file into the artifacts dir.

    Unlike ``store_artifact`` which takes ``bytes``, this takes a file path and
    moves the data chunkwise — no full RAM load. SHA256 is computed in the same
    pass to avoid a second read of the file.
    """
    file_path = Path(file_path)
    artifact_id = _generate_id("artifact")
    now = _format_time(_now())
    target_path = _artifact_path(artifact_id)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    h = hashlib.sha256()
    size = 0
    with file_path.open("rb") as src, target_path.open("wb") as dst:
        for chunk in iter(lambda: src.read(8192), b""):
            h.update(chunk)
            size += len(chunk)
            dst.write(chunk)

    checksum = h.hexdigest()

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO artifacts
            (artifact_id, task_id, stage_id, name, mime_type, size_bytes, checksum, storage_path, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                task_id,
                stage_id,
                name,
                mime_type,
                size,
                checksum,
                str(target_path),
                created_by,
                now,
            ),
        )
        conn.commit()
        event_bus.publish_sync(
            "artifact_created",
            {"artifact_id": artifact_id, "task_id": task_id, "created_by": created_by},
        )
        return {
            "artifact_id": artifact_id,
            "name": name,
            "path": str(target_path),
            "size_bytes": size,
            "mime_type": mime_type,
            "created_by": created_by,
        }
    finally:
        conn.close()


def get_artifact_metadata(artifact_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if not row:
            return None
        return _artifact_row_to_dict(row)
    finally:
        conn.close()


def list_artifacts(
    task_id: Optional[str] = None, stage_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        if task_id and stage_id:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? AND stage_id = ? ORDER BY created_at DESC",
                (task_id, stage_id),
            ).fetchall()
        elif task_id:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at DESC",
                (task_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM artifacts ORDER BY created_at DESC").fetchall()
        return [_artifact_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def delete_artifact(artifact_id: str) -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT storage_path FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if not row:
            return False
        path = Path(row["storage_path"])
        if path.exists():
            path.unlink()
        conn.execute("DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def cleanup_orphaned_artifacts(max_age_days: float = 7.0) -> Dict[str, Any]:
    """Delete artifacts whose ``task_id`` no longer refers to an existing task (T-049).

    Only artifacts older than ``max_age_days`` are considered, so recently
    created artifacts (e.g. during a running task) are never touched even
    if the task row briefly disappears.

    Returns ``{"deleted": n, "freed_bytes": m}``. File-system errors are
    logged but do not abort the run — the DB row is removed regardless so
    a dangling file can be cleaned up by a later ``db_vacuum``.
    """
    from datetime import timedelta

    cutoff = _format_time(_now() - timedelta(days=max_age_days))
    freed_bytes = 0
    deleted = 0

    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT artifact_id, storage_path, size_bytes
            FROM artifacts
            WHERE task_id IS NOT NULL
              AND task_id NOT IN (SELECT task_id FROM tasks)
              AND created_at < ?
            """,
            (cutoff,),
        ).fetchall()
        if not rows:
            return {"deleted": 0, "freed_bytes": 0}

        for row in rows:
            artifact_id = row["artifact_id"]
            path_str = row["storage_path"]
            size = int(row["size_bytes"] or 0)
            # Best-effort file deletion — the DB row is removed either way.
            try:
                p = Path(path_str)
                if p.exists():
                    p.unlink()
            except OSError as exc:
                logger.warning("Could not delete artifact file %s: %s", path_str, exc)
            conn.execute(
                "DELETE FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            )
            deleted += 1
            freed_bytes += size
            event_bus.publish_sync(
                "artifact_deleted",
                {"artifact_id": artifact_id, "reason": "orphaned"},
            )
        conn.commit()
        return {"deleted": deleted, "freed_bytes": freed_bytes}
    finally:
        conn.close()


def _artifact_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "artifact_id": row["artifact_id"],
        "task_id": row["task_id"],
        "stage_id": row["stage_id"],
        "name": row["name"],
        "mime_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "checksum": row["checksum"],
        "storage_path": row["storage_path"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }
