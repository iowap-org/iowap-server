"""Discovery and heartbeat core logic."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from relay_server.config import settings
from relay_server.core.db import get_conn, sync_node_capabilities, q
from relay_server.core.events import event_bus
from relay_server.core.status import (
    StatusCategory,
    node_can_transition,
    node_statuses_in_category,
)


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


def _sync_node_routes(node_id: str, routes: List[Dict[str, Any]]) -> None:
    """Replace all *permanent* routes for a node (T-075). Called on each heartbeat.

    T-123: only permanent heartbeat routes (``expires_at IS NULL``) are
    replaced here. Temporary bridge routes registered via T-124 carry
    an ``expires_at`` TTL and a ``channel_id``; they must survive the
    node's next heartbeat so an in-flight upload/download channel keeps
    working while the node is busy. We delete only the permanent rows
    (``expires_at IS NULL``) before re-inserting the declared set.
    """
    conn = get_conn()
    try:
        conn.execute(
            q("DELETE FROM node_routes WHERE node_id = ? AND expires_at IS NULL", (node_id,))
        )
        for route in routes:
            conn.execute(
                q("INSERT INTO node_routes (node_id, path, method, auth, upstream, description) "
                "VALUES (?, ?, ?, ?, ?, ?)", (
                    node_id,
                    route.get("path", ""),
                    route.get("method", "GET").upper(),
                    route.get("auth", "session"),
                    route.get("upstream", ""),
                    route.get("description", ""),
                )),
            )
        conn.commit()
    finally:
        conn.close()


def heartbeat(
    node_id: str,
    load: Optional[float] = None,
    queue_depth: Optional[int] = None,
    available: Optional[bool] = None,
    endpoint: Optional[str] = None,
    capabilities: Optional[List[Dict[str, Any]]] = None,
    replace_capabilities: bool = False,
    node_name: Optional[str] = None,
    description: Optional[str] = None,
    routes: Optional[List[Dict[str, Any]]] = None,
    status: Optional[str] = None,
    load_cap: Optional[float] = None,
) -> bool:
    """Process a node heartbeat. Returns True if node was updated.

    If ``replace_capabilities`` is True, the full capabilities list is
    replaced instead of merged (used by worker nodes sending their
    complete capability set on chaque heartbeat).

    T-072: ``node_name`` and ``description`` are optional top-level
    fields that the node can set in its capability YAML and heartbeat
    to the server. They update the corresponding columns in the
    ``nodes`` table so ``node list``/``node info`` can display them.

    T-081: ``status`` is the explicit node status the node wants to
    transition to (e.g. ``busy`` / ``idle`` set by ``node-cli node
    busy``/``idle``). The transition is validated via the central
    registry (:func:`relay_server.core.status.can_transition`);
    invalid transitions are ignored. ``load_cap`` is the per-node
    load ceiling; when ``load >= load_cap`` for
    :data:`settings.auto_busy_consecutive_heartbeats` heartbeats in a
    row, the node is automatically transitioned to ``busy`` (and back
    to ``idle`` once the load drops).
    """
    conn = get_conn()
    merged = None
    was_offline = False
    first_heartbeat = False
    status_changed_to: Optional[str] = None
    try:
        row = conn.execute(
            q("SELECT node_id, status, available, capabilities, first_heartbeat_seen, "
            "consecutive_high_load FROM nodes WHERE node_id = ?", (node_id,)),
        ).fetchone()
        if not row:
            return False

        now = _format_time(_now())
        old_status = row["status"]
        updates = ["last_seen = ?"]
        params: List[Any] = [now]
        new_status = old_status
        consecutive_high_load = int(row["consecutive_high_load"] or 0)

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
        # T-072: node-level node_name + description overrides.
        if node_name is not None:
            updates.append("node_name = ?")
            params.append(node_name)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
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

        # T-081: explicit node status requested by the node
        # (e.g. `node-cli node busy`). Validate via the central
        # registry; silently ignore invalid transitions.
        if status is not None and status != old_status:
            if node_can_transition(old_status, status):
                new_status = status
                updates.append("status = ?")
                params.append(new_status)
                status_changed_to = new_status

        # T-081: auto-busy based on load. When the node's load stays
        # at or above its load_cap for N consecutive heartbeats, flip
        # it to "busy"; reset the counter (and revert to "idle" when
        # the node is currently busy) once the load drops back.
        if load is not None:
            cap = load_cap if load_cap is not None else None
            if cap is not None and cap > 0:
                if load >= cap:
                    consecutive_high_load += 1
                else:
                    consecutive_high_load = 0
                updates.append("consecutive_high_load = ?")
                params.append(consecutive_high_load)

                threshold = getattr(
                    settings, "auto_busy_consecutive_heartbeats", 3
                )
                if (
                    consecutive_high_load >= threshold
                    and new_status in ("online", "idle", "approved")
                ):
                    new_status = "busy"
                    updates.append("status = ?")
                    params.append(new_status)
                    status_changed_to = new_status
                elif (
                    consecutive_high_load == 0
                    and new_status == "busy"
                    and status is None
                ):
                    # Load fell back below the cap and the node did not
                    # explicitly request busy → revert to idle.
                    new_status = "idle"
                    updates.append("status = ?")
                    params.append(new_status)
                    status_changed_to = new_status

        # T-113: auto-busy based on queue_depth (GPU-agnostic). A node
        # with queue_depth >= 1 already has a task in flight (e.g. a GPU
        # job saturating the GPU while CPU load stays low), so it is busy
        # regardless of load. This is a stronger, immediate signal than
        # the load-based counter — an AI/ML node running one FLUX job
        # must not be handed a second job just because its CPU is idle.
        if queue_depth is not None:
            busy_from_queue = queue_depth >= 1
            if (
                busy_from_queue
                and new_status in ("online", "idle", "approved")
                and status is None
            ):
                new_status = "busy"
                updates.append("status = ?")
                params.append(new_status)
                status_changed_to = new_status
            elif (
                not busy_from_queue
                and new_status == "busy"
                and status is None
                and queue_depth == 0
                and consecutive_high_load == 0
            ):
                # Queue drained AND load is low (no load-based busy
                # pending) and the node did not explicitly request busy
                # → revert to idle. The load-based revert above already
                # handles the CPU-only case; this covers a node that was
                # marked busy solely by queue_depth.
                new_status = "idle"
                updates.append("status = ?")
                params.append(new_status)
                status_changed_to = new_status

        # If the node was marked offline, bring it back online.
        # Also transition from approved → online on any heartbeat so a
        # freshly approved node doesn't stay approved forever.
        was_offline = old_status == "offline"
        is_approved = old_status == "approved"
        if (was_offline or is_approved) and status_changed_to is None:
            new_status = "online"
            updates.append("status = ?")
            params.append(new_status)
            status_changed_to = new_status if old_status != new_status else None

        # Track first approved heartbeat for node_online semantics.
        is_approved = old_status in ("approved", "offline")
        first_heartbeat = is_approved and not row["first_heartbeat_seen"]
        if first_heartbeat:
            updates.append("first_heartbeat_seen = ?")
            params.append(1)

        params.append(node_id)
        sql = f"UPDATE nodes SET {', '.join(updates)} WHERE node_id = ?"
        conn.execute(q(sql, params))
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

    # T-075: sync routes — replace all routes for this node on each heartbeat.
    if routes is not None:
        try:
            _sync_node_routes(node_id, routes)
        except Exception:
            pass

    # Publish event when node comes back from offline or on its first
    # heartbeat after being approved.
    if was_offline or first_heartbeat:
        event_bus.publish_sync("node_online", {"node_id": node_id})

    # T-082: publish a status_changed event for any node status
    # transition that happened during this heartbeat (explicit request
    # via the ``status`` field, auto-busy, or the approved/offline →
    # online recovery above).
    if status_changed_to is not None and status_changed_to != old_status:
        event_bus.publish_sync(
            "status_changed",
            {
                "entity_type": "node",
                "entity_id": node_id,
                "old_status": old_status,
                "new_status": status_changed_to,
            },
        )

    return True


def list_nodes(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List registered nodes, optionally filtered by status.

    Special keyword ``status="all"`` disables filtering and returns every node
    (useful for admin/CLI views that want to include offline/pending nodes).
    """
    conn = get_conn()
    try:
        if status and status.lower() != "all":
            rows = conn.execute(
                q("SELECT node_id, node_name, description, endpoint, capabilities, load, queue_depth, "
                "available, last_seen, registered_at, status, role "
                "FROM nodes WHERE status = ? ORDER BY registered_at DESC", (status,)),
            ).fetchall()
        else:
            rows = conn.execute(
                q("SELECT node_id, node_name, description, endpoint, capabilities, load, queue_depth, "
                "available, last_seen, registered_at, status, role "
                "FROM nodes ORDER BY registered_at DESC")
            ).fetchall()

        return [_node_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def get_node(node_id: str) -> Optional[Dict[str, Any]]:
    """Get a single node by ID."""
    conn = get_conn()
    try:
        row = conn.execute(
            q("SELECT node_id, node_name, description, endpoint, capabilities, load, queue_depth, "
            "available, last_seen, registered_at, status, role "
            "FROM nodes WHERE node_id = ?", (node_id,)),
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
            q("""
            SELECT node_id, node_name, description, endpoint, capabilities, load, queue_depth,
                   available, last_seen, registered_at, status, role
            FROM nodes
            WHERE status IN ('approved', 'online')
              AND last_seen > ?
            ORDER BY load ASC, queue_depth ASC
            """, (threshold,)),
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

    T-058: The authoritative source is the normalized ``node_capabilities``
    index, which is rebuilt from scratch (DELETE + INSERT) on every
    heartbeat with ``replace_capabilities=True``. Stale entries that exist
    only in the legacy ``nodes.capabilities`` JSON column but were never
    confirmed by a heartbeat are therefore no longer surfaced. The JSON
    column is only consulted as a fallback to recover fields not stored
    in the index (``config``) and to cover nodes that have not yet been
    synced into ``node_capabilities``.
    """
    threshold = _format_time(_node_timeout_threshold())
    conn = get_conn()
    try:
        rows = conn.execute(
            q("""
            SELECT node_id, node_name, description, endpoint, capabilities, load,
                   queue_depth, available, last_seen, status, role
            FROM nodes
            WHERE status IN ('approved', 'online')
              AND (last_seen > ? OR available = 0)
            ORDER BY load ASC
            """, (threshold,)),
        ).fetchall()

        # Capabilities sammeln: name -> {type, description, version, nodes}
        cap_map: dict[str, dict] = {}

        for row in rows:
            node_available = bool(row["available"])

            # T-058: read from the normalized node_capabilities index,
            # which only contains capabilities confirmed by a heartbeat
            # (sync_node_capabilities does DELETE+INSERT on every replace).
            nc_rows = conn.execute(
                q("SELECT capability_name, capability_type, capability_version, "
                "description, input_schema, available "
                "FROM node_capabilities WHERE node_id = ?", (row["node_id"],)),
            ).fetchall()

            if nc_rows:
                # Index is authoritative – build cap dicts from it, then
                # enrich with config from the legacy JSON column (this field
                # is not stored in node_capabilities).
                legacy_by_name = {
                    c.get("name"): c
                    for c in _parse_capabilities(row["capabilities"])
                    if isinstance(c, dict) and c.get("name")
                }
                caps = []
                for nc in nc_rows:
                    legacy = legacy_by_name.get(nc["capability_name"], {})
                    schema = None
                    schema_raw = nc["input_schema"]
                    if schema_raw:
                        try:
                            schema = json.loads(schema_raw)
                        except Exception:
                            schema = None
                    caps.append({
                        "name": nc["capability_name"],
                        "type": nc["capability_type"] or "",
                        "version": nc["capability_version"] or "1.0.0",
                        "description": nc["description"] or "",
                        "available": bool(nc["available"]),
                        "input_schema": schema,
                        "config": legacy.get("config", {}),
                    })
            else:
                # Fallback: node has never been heartbeated into the index
                # (e.g. freshly registered / migrated node). Use the JSON
                # column so the capability is not invisible until the next
                # heartbeat arrives.
                caps = _parse_capabilities(row["capabilities"])

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
                        "input_schema": cap.get("input_schema"),
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
    """Mark alive nodes as offline if heartbeat timeout exceeded (T-080).

    Admin nodes do not send heartbeats and are therefore excluded.
    Uses a re-check in the UPDATE WHERE clause to avoid TOCTOU races:
    a node that heartbeats between SELECT and UPDATE will not be marked offline.

    T-080: the set of statuses eligible for offline-marking is derived
    from the central registry — every node status in the AVAILABLE or
    BUSY category (approved, online, idle, busy, maintenance) is a live
    node that should be flagged offline when it stops heartbeating.
    PENDING (``pending``) nodes have never heartbeated and are left
    alone; OFFLINE nodes are already offline.

    T-061: when a node is marked offline, all of its ``claimed`` stages
    are transitioned to ``failed`` (and the owning task is failed too
    if every stage has reached a terminal state). This prevents stages
    from staying ``claimed`` forever when a node dies without releasing
    its claims.

    T-082: a ``status_changed`` event is published for every node and
    stage transitioned here.
    """
    threshold = _format_time(_node_timeout_threshold())
    # AVAILABLE + BUSY node statuses (approved, online, idle, busy, maintenance).
    live_statuses = node_statuses_in_category(StatusCategory.AVAILABLE) + \
        node_statuses_in_category(StatusCategory.BUSY)
    placeholders = ",".join("?" for _ in live_statuses) or "''"
    conn = get_conn()
    try:
        rows = conn.execute(
            q(f"""
            SELECT node_id FROM nodes
            WHERE status IN ({placeholders}) AND last_seen < ? AND role != 'admin'
            """, [*live_statuses, threshold]),
        ).fetchall()
        candidate_ids = [r["node_id"] for r in rows]
        if not candidate_ids:
            return []

        # Capture the old status of each candidate so we can publish
        # accurate status_changed events after the UPDATE.
        old_status_map: dict[str, str] = {
            nid: conn.execute(
                q("SELECT status FROM nodes WHERE node_id = ?", (nid,))
            ).fetchone()["status"]
            for nid in candidate_ids
        }

        # Re-check last_seen in the UPDATE to avoid TOCTOU:
        # only mark offline if last_seen is STILL below threshold.
        # SQLAlchemy 2.0: pass a list of param dicts to a single execute()
        # for executemany semantics (Connection.executemany was removed).
        conn.execute(
            q("""
            UPDATE nodes SET status = 'offline', available = 0
            WHERE node_id = ? AND last_seen < ?
            """),
            [{"p0": nid, "p1": threshold} for nid in candidate_ids],
        )

        # Determine which nodes were actually updated (the UPDATE may have
        # matched 0 rows if a heartbeat came in between SELECT and UPDATE).
        offline_ids = [
            nid for nid in candidate_ids
            if conn.execute(
                q("SELECT status FROM nodes WHERE node_id = ?", (nid,))
            ).fetchone()["status"] == "offline"
        ]

        # T-061: fail claimed stages owned by the now-offline nodes.
        failed_stages: list[dict] = []
        affected_tasks: set[str] = set()
        if offline_ids:
            placeholders = ",".join("?" for _ in offline_ids)
            stage_rows = conn.execute(
                q(f"SELECT stage_id, task_id FROM task_stages "
                f"WHERE status = 'claimed' AND claimed_by IN ({placeholders})", offline_ids),
            ).fetchall()
            now = _format_time(_now())
            for r in stage_rows:
                conn.execute(
                    q("""
                    UPDATE task_stages
                    SET status = 'failed', claimed_by = NULL, claimed_at = NULL,
                        claim_expires_at = NULL, updated_at = ?
                    WHERE stage_id = ?
                    """, (now, r["stage_id"])),
                )
                failed_stages.append({"stage_id": r["stage_id"], "task_id": r["task_id"]})
                affected_tasks.add(r["task_id"])

            # Fail tasks where every stage has reached a terminal state.
            tasks_failed: list[str] = []
            for task_id in affected_tasks:
                remaining = conn.execute(
                    q("SELECT COUNT(*) FROM task_stages "
                    "WHERE task_id = ? AND status NOT IN ('completed', 'failed', 'timed_out')", (task_id,)),
                ).fetchone()[0]
                if remaining == 0:
                    conn.execute(
                        q("UPDATE tasks SET status = 'failed', updated_at = ?, completed_at = ? "
                        "WHERE task_id = ? AND status NOT IN ('failed', 'completed', 'timed_out')", (now, now, task_id)),
                    )
                    tasks_failed.append(task_id)

        conn.commit()

        # T-075: clear routes for offline nodes.
        for nid in offline_ids:
            conn.execute(q("DELETE FROM node_routes WHERE node_id = ?", (nid,)))
        conn.commit()

        for nid in offline_ids:
            event_bus.publish_sync("node_offline", {"node_id": nid})
            event_bus.publish_sync(
                "status_changed",
                {
                    "entity_type": "node",
                    "entity_id": nid,
                    "old_status": old_status_map.get(nid),
                    "new_status": "offline",
                },
            )
        for s in failed_stages:
            event_bus.publish_sync(
                "stage_failed",
                {"stage_id": s["stage_id"], "task_id": s["task_id"]},
            )
            event_bus.publish_sync(
                "status_changed",
                {
                    "entity_type": "stage",
                    "entity_id": s["stage_id"],
                    "old_status": "claimed",
                    "new_status": "failed",
                },
            )
        for task_id in tasks_failed:
            event_bus.publish_sync("task_failed", {"task_id": task_id})
            event_bus.publish_sync(
                "status_changed",
                {
                    "entity_type": "task",
                    "entity_id": task_id,
                    "old_status": "running",
                    "new_status": "failed",
                },
            )

        return offline_ids
    finally:
        conn.close()


def _node_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "node_id": row["node_id"],
        "node_name": row["node_name"],
        "description": row["description"] if "description" in row.keys() else None,
        "capabilities": _parse_capabilities(row["capabilities"]),
        "load": row["load"],
        "queue_depth": row["queue_depth"],
        "available": bool(row["available"]),
        "last_seen": row["last_seen"],
        "registered_at": row["registered_at"],
        "status": row["status"],
        "role": row["role"],
    }
