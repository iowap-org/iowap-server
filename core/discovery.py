"""Discovery and heartbeat core logic."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from relay_server.config import settings
from relay_server.core.db import get_conn, sync_node_capabilities
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
    replace_capabilities: bool = False,
) -> bool:
    """Process a node heartbeat. Returns True if node was updated.

    If ``replace_capabilities`` is True, the full capabilities list is
    replaced instead of merged (used by worker nodes sending their
    complete capability set on chaque heartbeat).
    """
    conn = get_conn()
    merged = None
    was_offline = False
    first_heartbeat = False
    try:
        row = conn.execute(
            "SELECT node_id, status, available, capabilities, first_heartbeat_seen FROM nodes WHERE node_id = ?",
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
            if replace_capabilities:
                # Full replace – worker sends complete capability set
                updates.append("capabilities = ?")
                params.append(_serialize_capabilities(capabilities))
                merged = capabilities
            else:
                # Merge – update existing capabilities list
                existing = _parse_capabilities(row["capabilities"])
                cap_map = {c.get("name"): c for c in existing if isinstance(c, dict)}
                for cap in capabilities:
                    if isinstance(cap, dict) and cap.get("name"):
                        cap_map[cap["name"]] = cap
                merged = list(cap_map.values())
                updates.append("capabilities = ?")
                params.append(_serialize_capabilities(merged))
        else:
            merged = None

        # If the node was marked offline, bring it back online.
        # Also transition from approved → online on any heartbeat so a
        # freshly approved node doesn't stay approved forever.
        was_offline = row["status"] == "offline"
        is_approved = row["status"] == "approved"
        if was_offline or is_approved:
            updates.append("status = ?")
            params.append("online")

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
    finally:
        conn.close()

    # Keep the normalized node_capabilities index in sync (T-026).
    # Done after conn.close() so sync_node_capabilities can open its own
    # connection without contending for the same SQLite handle.
    if merged is not None:
        try:
            sync_node_capabilities(node_id, merged)
        except Exception:
            # Best-effort: a stale index is self-healing on the next
            # heartbeat; the authoritative source remains the JSON column.
            pass

    # Publish event when node comes back from offline or on its first
    # heartbeat after being approved.
    if was_offline or first_heartbeat:
        event_bus.publish_sync("node_online", {"node_id": node_id})

    return True


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
            WHERE status IN ('approved', 'online')
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


def get_capabilities(
    capability_name: Optional[str] = None,
    type_filter: Optional[str] = None,
    available_only: bool = True,
    config_filter: Optional[Dict[str, Any]] = None,
) -> list[dict]:
    """
    Returns all capabilities of all active nodes,
    grouped by capability name.

    Each capability contains the nodes that offer it.

    ``config_filter`` allows filtering by config values, e.g.
    ``{"region": "eu-west"}`` – only nodes are returned
    whose capability config contains all the specified key-value pairs.
    """
    threshold = _format_time(_node_timeout_threshold())
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT node_id, node_name, endpoint, capabilities, load,
                   queue_depth, available, last_seen, status, role
            FROM nodes
            WHERE status IN ('approved', 'online')
              AND (last_seen > ? OR available = 0)
            ORDER BY load ASC
            """,
            (threshold,),
        ).fetchall()

        # Capabilities sammeln: name -> {type, description, version, nodes}
        cap_map: dict[str, dict] = {}

        for row in rows:
            caps = _parse_capabilities(row["capabilities"])
            node_available = bool(row["available"])

            for cap in caps:
                name: str = cap.get("name", "")
                if not name:
                    continue

                # Filter: only a specific capability?
                if capability_name and name != capability_name:
                    continue

                # Filter: only a specific type?
                cap_type = cap.get("type", "")
                if type_filter and cap_type != type_filter:
                    continue

                # Filter: only available ones?
                if available_only and not node_available:
                    continue

                # Filter: config-basiert?
                if config_filter:
                    cap_config = cap.get("config", {})
                    if not all(
                        cap_config.get(k) == v
                        for k, v in config_filter.items()
                    ):
                        continue

                if name not in cap_map:
                    cap_map[name] = {
                        "name": name,
                        "type": cap_type,
                        "description": cap.get("description", ""),
                        "version": cap.get("version", "1.0.0"),
                        "available": node_available,
                        "input_schema": cap.get("input"),
                        "nodes": [],
                    }

                cap_map[name]["nodes"].append({
                    "node_id": row["node_id"],
                    "node_name": row["node_name"],
                    "available": node_available,
                    "load": row["load"] or 0.0,
                    "queue_depth": row["queue_depth"] or 0,
                    "last_seen": row["last_seen"],
                    "config": cap.get("config", {}),
                })

            # If this node is not available,
            # override the availability of its caps
            # BUT only if no OTHER node still has the cap available.
            if not node_available:
                for c in caps:
                    cname = c.get("name", "")
                    if cname in cap_map:
                        # Check if any other node in cap_map[cname]["nodes"]
                        # still has available=True
                        other_available = any(
                            n["available"]
                            for n in cap_map[cname]["nodes"]
                            if n["node_id"] != row["node_id"]
                        )
                        if not other_available:
                            cap_map[cname]["available"] = False

        return list(cap_map.values())

    finally:
        conn.close()


def get_capability_by_name(name: str) -> Optional[dict]:
    """Return a single capability with all of its nodes."""
    all_caps = get_capabilities(capability_name=name, available_only=False)
    if not all_caps:
        return None
    return all_caps[0]


def mark_offline_nodes() -> List[str]:
    """Mark approved/online nodes as offline if heartbeat timeout exceeded.

    Admin nodes do not send heartbeats and are therefore excluded.
    Uses a re-check in the UPDATE WHERE clause to avoid TOCTOU races:
    a node that heartbeats between SELECT and UPDATE will not be marked offline.
    """
    threshold = _format_time(_node_timeout_threshold())
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT node_id FROM nodes
            WHERE status IN ('approved', 'online') AND last_seen < ? AND role != 'admin'
            """,
            (threshold,),
        ).fetchall()
        candidate_ids = [r["node_id"] for r in rows]
        if not candidate_ids:
            return []

        # Re-check last_seen in the UPDATE to avoid TOCTOU:
        # only mark offline if last_seen is STILL below threshold.
        conn.executemany(
            """
            UPDATE nodes SET status = 'offline', available = 0
            WHERE node_id = ? AND last_seen < ?
            """,
            [(nid, threshold) for nid in candidate_ids],
        )
        conn.commit()

        # Determine which nodes were actually updated (the UPDATE may have
        # matched 0 rows if a heartbeat came in between SELECT and UPDATE).
        offline_ids = [
            nid for nid in candidate_ids
            if conn.execute(
                "SELECT status FROM nodes WHERE node_id = ?", (nid,)
            ).fetchone()["status"] == "offline"
        ]

        for nid in offline_ids:
            event_bus.publish_sync("node_offline", {"node_id": nid})
        return offline_ids
    finally:
        conn.close()


def _node_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "node_id": row["node_id"],
        "node_name": row["node_name"],
        "capabilities": _parse_capabilities(row["capabilities"]),
        "load": row["load"],
        "queue_depth": row["queue_depth"],
        "available": bool(row["available"]),
        "last_seen": row["last_seen"],
        "registered_at": row["registered_at"],
        "status": row["status"],
        "role": row["role"],
    }
