"""Artifact storage core logic — keeps metadata in DB, files in artifacts_dir."""

import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from relay_server.config import settings
from relay_server.core.db import get_conn
from relay_server.core.events import event_bus


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
