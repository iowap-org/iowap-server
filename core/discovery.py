"""Discovery and heartbeat core logic."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from relay_server.config import settings
from relay_server.core.db import get_conn
from relay_server.core.events import event_bus


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(dt: datetime) -> str:
    return dt.isoformat()


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _serialize_capabilities(capabilities: List[Dict[str, Any]]) -> str:
    return json.dumps(capabilities)


def _parse_capabilities(value: Optional[str]) -> List[Dict[str, Any]]:
    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        return []


def _node_timeout_threshold() -> datetime:
    seconds = settings.heartbeat_interval_seconds * settings.heartbeat_timeout_multiplier
    return _now() - timedelta(seconds=seconds)


def heartbeat(
    node_id: str,
    load: Optional[float] = None,
    queue_depth: Optional[int] = None,
    available: Optional[bool] = None,
    endpoint: Optional[str] = None,
    capabilities: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    """Process a node heartbeat. Returns True if node was updated."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT node_id, status, available, first_heartbeat_seen FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if not row:
            return False

        now = _format_time(_now())
        updates = ["last_seen = ?"]
        params: List[Any] = [now]

        if load is not None:
            updates.append("load = ?")
            params.append(load)
        if queue_depth is not None:
            updates.append("queue_depth = ?")
            params.append(queue_depth)
        if available is not None:
            updates.append("available = ?")
            params.append(1 if available else 0)
        if endpoint is not None:
            updates.append("endpoint = ?")
            params.append(endpoint)
        if capabilities is not None:
            updates.append("capabilities = ?")
            params.append(_serialize_capabilities(capabilities))

        # If the node was marked offline, bring it back online.
        was_offline = row["status"] == "offline"
        if was_offline:
            updates.append("status = ?")
            params.append("approved")

        # Track first approved heartbeat for node_online semantics.
        is_approved = row["status"] in ("approved", "offline")
        first_heartbeat = is_approved and not row["first_heartbeat_seen"]
        if first_heartbeat:
            updates.append("first_heartbeat_seen = ?")
            params.append(1)

        params.append(node_id)
        sql = f"UPDATE nodes SET {', '.join(updates)} WHERE node_id = ?"
        conn.execute(sql, params)
        conn.commit()

        # Publish event when node comes back from offline or on its first
        # heartbeat after being approved.
        if was_offline or first_heartbeat:
            event_bus.publish_sync("node_online", {"node_id": node_id})

        return True
    finally:
        conn.close()


def list_nodes(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List registered nodes, optionally filtered by status."""
    conn = get_conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT node_id, node_name, endpoint, capabilities, load, queue_depth, "
                "available, last_seen, registered_at, status, role "
                "FROM nodes WHERE status = ? ORDER BY registered_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT node_id, node_name, endpoint, capabilities, load, queue_depth, "
                "available, last_seen, registered_at, status, role "
                "FROM nodes ORDER BY registered_at DESC"
            ).fetchall()

        return [_node_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_node(node_id: str) -> Optional[Dict[str, Any]]:
    """Get a single node by ID."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT node_id, node_name, endpoint, capabilities, load, queue_depth, "
            "available, last_seen, registered_at, status, role "
            "FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        return _node_row_to_dict(row) if row else None
    finally:
        conn.close()


def query_nodes_by_capability(capability: str) -> List[Dict[str, Any]]:
    """Return approved, online nodes that advertise a given capability."""
    threshold = _format_time(_node_timeout_threshold())
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT node_id, node_name, endpoint, capabilities, load, queue_depth,
                   available, last_seen, registered_at, status, role
            FROM nodes
            WHERE status = 'approved'
              AND last_seen > ?
            ORDER BY load ASC, queue_depth ASC
            """,
            (threshold,),
        ).fetchall()

        matching = []
        for row in rows:
            caps = _parse_capabilities(row["capabilities"])
            if any(c.get("name") == capability for c in caps):
                matching.append(_node_row_to_dict(row))
        return matching
    finally:
        conn.close()


def mark_offline_nodes() -> List[str]:
    """Mark approved nodes as offline if heartbeat timeout exceeded."""
    threshold = _format_time(_node_timeout_threshold())
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT node_id FROM nodes
            WHERE status = 'approved' AND last_seen < ?
            """,
            (threshold,),
        ).fetchall()
        offline_ids = [r["node_id"] for r in rows]
        if offline_ids:
            conn.executemany(
                "UPDATE nodes SET status = 'offline', available = 0 WHERE node_id = ?",
                [(nid,) for nid in offline_ids],
            )
            conn.commit()
            for nid in offline_ids:
                event_bus.publish_sync("node_offline", {"node_id": nid})
        return offline_ids
    finally:
        conn.close()


def _node_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "node_id": row["node_id"],
        "node_name": row["node_name"],
        "endpoint": row["endpoint"],
        "capabilities": _parse_capabilities(row["capabilities"]),
        "load": row["load"],
        "queue_depth": row["queue_depth"],
        "available": bool(row["available"]),
        "last_seen": row["last_seen"],
        "registered_at": row["registered_at"],
        "status": row["status"],
        "role": row["role"],
    }
