"""Presence core logic."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from relay_server.core.db import get_conn
from relay_server.core.events import event_bus


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(dt: datetime) -> str:
    return dt.isoformat()


def _parse_json(value: Optional[str]) -> Optional[dict]:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def update_presence(
    node_id: str,
    status: Optional[str] = None,
    mood: Optional[str] = None,
    activity: Optional[Dict[str, Any]] = None,
    progress: Optional[int] = None,
    eta_seconds: Optional[int] = None,
    next_available: Optional[str] = None,
) -> bool:
    """Update presence record for a node. Returns True if updated."""
    conn = get_conn()
    try:
        # Ensure the node exists.
        node = conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
        if not node:
            return False

        now = _format_time(_now())
        conn.execute(
            """
            INSERT INTO presence (node_id, status, mood, activity_json, progress,
                                  eta_seconds, next_available, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                status = COALESCE(?, status),
                mood = COALESCE(?, mood),
                activity_json = COALESCE(?, activity_json),
                progress = COALESCE(?, progress),
                eta_seconds = COALESCE(?, eta_seconds),
                next_available = COALESCE(?, next_available),
                updated_at = ?
            """,
            (
                node_id,
                status,
                mood,
                json.dumps(activity) if activity else None,
                progress,
                eta_seconds,
                next_available,
                now,
                status,
                mood,
                json.dumps(activity) if activity else None,
                progress,
                eta_seconds,
                next_available,
                now,
            ),
        )
        conn.commit()
        event_bus.publish_sync("presence_changed", {"node_id": node_id, "status": status})
        return True
    finally:
        conn.close()


def get_presence(node_id: str) -> Optional[Dict[str, Any]]:
    """Get presence for a single node."""
    conn = get_conn()
    try:
        row = conn.execute(
            """
            SELECT p.node_id, p.status, p.mood, p.activity_json, p.progress,
                   p.eta_seconds, p.next_available, p.updated_at,
                   n.node_name, n.endpoint, n.capabilities
            FROM presence p
            JOIN nodes n ON n.node_id = p.node_id
            WHERE p.node_id = ?
            """,
            (node_id,),
        ).fetchone()
        return _presence_row_to_dict(row) if row else None
    finally:
        conn.close()


def list_presence(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List presence records, optionally filtered by status."""
    conn = get_conn()
    try:
        if status:
            rows = conn.execute(
                """
                SELECT p.node_id, p.status, p.mood, p.activity_json, p.progress,
                       p.eta_seconds, p.next_available, p.updated_at,
                       n.node_name, n.endpoint, n.capabilities
                FROM presence p
                JOIN nodes n ON n.node_id = p.node_id
                WHERE p.status = ?
                ORDER BY p.updated_at DESC
                """,
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT p.node_id, p.status, p.mood, p.activity_json, p.progress,
                       p.eta_seconds, p.next_available, p.updated_at,
                       n.node_name, n.endpoint, n.capabilities
                FROM presence p
                JOIN nodes n ON n.node_id = p.node_id
                ORDER BY p.updated_at DESC
                """,
            ).fetchall()
        return [_presence_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _presence_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "node_id": row["node_id"],
        "node_name": row["node_name"],
        "endpoint": row["endpoint"],
        "capabilities": _parse_capabilities(row["capabilities"]),
        "status": row["status"],
        "mood": row["mood"],
        "activity": _parse_json(row["activity_json"]),
        "progress": row["progress"],
        "eta_seconds": row["eta_seconds"],
        "next_available": row["next_available"],
        "updated_at": row["updated_at"],
    }


def _parse_capabilities(value: Optional[str]) -> List[Dict[str, Any]]:
    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        return []
