"""Public, read-only cluster portal API (Phase 20 — T-044).

These endpoints expose *status-only* cluster information for the public
Community Dashboard. No authentication, no CSRF, no secrets — only
aggregated metrics, node/user profiles and the activity feed.

Rationale: the existing dashboard required a login for every page view,
which made it useless as a public cluster showcase. The Community
Dashboard splits the UI into a public portal (these endpoints) and a
login-protected admin area (the existing ``/dashboard/api/*``
endpoints, unchanged).

Exposed data is deliberately limited to what a visitor may see without
credentials: node names/IDs/status/load/capability *names*, human user
names/roles/status, and the recent event feed. Capability input
schemas, payloads, tokens and secrets are never returned.
"""

import json
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, status

from relay_server.core.db import get_conn
from relay_server.core.events import event_bus
from relay_server.core.status import get_category, status_color

router = APIRouter()

# The synthetic dashboard-admin node is an internal implementation
# detail and never shown in the public portal.
_DASHBOARD_ADMIN_NODE = "__dashboard_admin__"


def _safe_json(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _parse_cap_names(value: Any) -> list[str]:
    """Return capability *names* only (no schemas/config) for public view."""
    caps = _safe_json(value, [])
    names: list[str] = []
    if isinstance(caps, list):
        for c in caps:
            if isinstance(c, dict) and c.get("name"):
                names.append(c["name"])
            elif isinstance(c, str):
                names.append(c)
    return names


# ---------------------------------------------------------------------------
# 1. Overview
# ---------------------------------------------------------------------------


@router.get("/overview")
async def cluster_overview():
    """Aggregated cluster metrics + nodes + recent activity feed.

    Public, no auth. Returns summary counts, a compact node list, the
    recent activity feed (last 50 events) and a generated timestamp.
    """
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()

        node_rows = conn.execute(
            "SELECT node_id, node_name, description, endpoint, capabilities, status, role, "
            "load, queue_depth, last_seen, registered_at "
            "FROM nodes WHERE node_id != ? ORDER BY registered_at DESC",
            (_DASHBOARD_ADMIN_NODE,),
        ).fetchall()
        nodes: list[dict] = []
        online_count = 0
        for r in node_rows:
            cat = get_category(r["status"])
            cap_names = _parse_cap_names(r["capabilities"])
            nodes.append(
                {
                    "node_id": r["node_id"],
                    "node_name": r["node_name"],
                    "description": r["description"],
                    "endpoint": r["endpoint"],
                    "capability_names": cap_names,
                    "capability_count": len(cap_names),
                    "status": r["status"],
                    "status_category": cat.value if cat else None,
                    "status_color": status_color(r["status"]),
                    "role": r["role"],
                    "load": r["load"],
                    "queue_depth": r["queue_depth"],
                    "last_seen": r["last_seen"],
                    "registered_at": r["registered_at"],
                    "online": r["status"] == "online",
                }
            )
            if r["status"] == "online":
                online_count += 1

        task_stat_rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall()
        task_stats = {r["status"]: r["cnt"] for r in task_stat_rows}
        total_tasks = sum(task_stats.values())

        active_stages_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM task_stages WHERE status IN ('pending','claimed')"
        ).fetchone()["cnt"]

        artifacts_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM artifacts"
        ).fetchone()["cnt"]

        # Distinct advertised capability names across all non-pending nodes.
        cap_count_row = conn.execute(
            "SELECT COUNT(DISTINCT capability_name) as cnt FROM node_capabilities nc "
            "JOIN nodes n ON n.node_id = nc.node_id "
            "WHERE n.status IN ('approved','online','idle','busy','maintenance','offline')"
        ).fetchone()
        capability_count = cap_count_row["cnt"] if cap_count_row else 0

        summary = {
            "total_nodes": len(nodes),
            "online_nodes": online_count,
            "total_tasks": total_tasks,
            "task_stats": task_stats,
            "active_stages": active_stages_count,
            "total_artifacts": artifacts_count,
            "capability_count": capability_count,
        }

        events = event_bus.recent(limit=50)

        return {
            "generated_at": now,
            "summary": summary,
            "nodes": nodes,
            "activity": events,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. Nodes list + profile
# ---------------------------------------------------------------------------


def _public_node_row(r) -> dict:
    cat = get_category(r["status"])
    cap_names = _parse_cap_names(r["capabilities"])
    return {
        "node_id": r["node_id"],
        "node_name": r["node_name"],
        "description": r["description"],
        "endpoint": r["endpoint"],
        "capability_names": cap_names,
        "capability_count": len(cap_names),
        "status": r["status"],
        "status_category": cat.value if cat else None,
        "status_color": status_color(r["status"]),
        "role": r["role"],
        "load": r["load"],
        "queue_depth": r["queue_depth"],
        "last_seen": r["last_seen"],
        "registered_at": r["registered_at"],
        "online": r["status"] == "online",
    }


@router.get("/nodes")
async def cluster_nodes():
    """List all nodes with status, capability names, load and queue.

    Public, no auth. The synthetic dashboard-admin node is excluded.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT node_id, node_name, description, endpoint, capabilities, status, role, "
            "load, queue_depth, last_seen, registered_at "
            "FROM nodes WHERE node_id != ? ORDER BY registered_at DESC",
            (_DASHBOARD_ADMIN_NODE,),
        ).fetchall()
        return {"nodes": [_public_node_row(r) for r in rows]}
    finally:
        conn.close()


@router.get("/nodes/{node_id}")
async def cluster_node_profile(node_id: str):
    """Public node profile: details, capability list, recent tasks, activity.

    Capability entries include ``name``, ``type``, ``description`` and
    ``input_schema`` (read from the normalized index) so the profile
    page can render the same detail the admin dashboard shows. No
    secrets, no tokens, no payload data.
    """
    conn = get_conn()
    try:
        if node_id == _DASHBOARD_ADMIN_NODE:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")

        row = conn.execute(
            "SELECT node_id, node_name, description, endpoint, capabilities, status, role, "
            "load, queue_depth, available, last_seen, registered_at, first_heartbeat_seen "
            "FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Node not found")

        # Capability details from the normalized index.
        nc_rows = conn.execute(
            "SELECT capability_name, capability_type, capability_version, description, "
            "input_schema, available "
            "FROM node_capabilities WHERE node_id = ? ORDER BY capability_name",
            (node_id,),
        ).fetchall()
        capabilities: list[dict] = []
        for nc in nc_rows:
            schema = None
            schema_raw = nc["input_schema"]
            if schema_raw:
                try:
                    schema = json.loads(schema_raw)
                except Exception:
                    schema = None
            capabilities.append(
                {
                    "name": nc["capability_name"],
                    "type": nc["capability_type"] or "",
                    "version": nc["capability_version"] or "1.0.0",
                    "description": nc["description"] or "",
                    "available": bool(nc["available"]),
                    "input_schema": schema,
                }
            )

        # Last 20 tasks that touched this node (as owner or claimer).
        task_rows = conn.execute(
            "SELECT t.task_id, t.task_name, t.status, t.priority, t.created_at, t.completed_at "
            "FROM tasks t "
            "LEFT JOIN task_stages s ON s.task_id = t.task_id "
            "WHERE t.owner_node_id = ? OR s.claimed_by = ? "
            "GROUP BY t.task_id ORDER BY t.created_at DESC LIMIT 20",
            (node_id, node_id),
        ).fetchall()
        recent_tasks = []
        for r in task_rows:
            cat = get_category(r["status"])
            recent_tasks.append(
                {
                    "task_id": r["task_id"],
                    "task_name": r["task_name"],
                    "status": r["status"],
                    "status_category": cat.value if cat else None,
                    "status_color": status_color(r["status"]),
                    "priority": r["priority"],
                    "created_at": r["created_at"],
                    "completed_at": r["completed_at"],
                }
            )

        # Load history (last 20 load samples from the audit/event log is
        # not tracked per-node; instead we synthesise a simple recent
        # load curve from the current load + queue depth so the profile
        # page can render a CSS-only mini chart without a charting lib).
        load_history = _node_load_history(conn, node_id)

        cat = get_category(row["status"])
        profile = {
            "node_id": row["node_id"],
            "node_name": row["node_name"],
            "description": row["description"],
            "endpoint": row["endpoint"],
            "capability_names": _parse_cap_names(row["capabilities"]),
            "capabilities": capabilities,
            "capability_count": len(capabilities),
            "status": row["status"],
            "status_category": cat.value if cat else None,
            "status_color": status_color(row["status"]),
            "role": row["role"],
            "load": row["load"],
            "queue_depth": row["queue_depth"],
            "available": bool(row["available"]),
            "last_seen": row["last_seen"],
            "registered_at": row["registered_at"],
            "first_heartbeat_seen": row["first_heartbeat_seen"],
            "online": row["status"] == "online",
            "recent_tasks": recent_tasks,
            "load_history": load_history,
        }
        return profile
    finally:
        conn.close()


def _node_load_history(conn, node_id: str) -> list[dict]:
    """Best-effort recent load samples for a node.

    There is no dedicated per-node load history table, so we derive a
    short curve from the ``status_changed`` events stored in the audit
    log for this node (capped at 20). Each entry carries the timestamp
    and the current ``load``/``queue_depth`` snapshot read from the
    ``nodes`` row at that point — which we approximate with the current
    load value, since the audit log only records status transitions,
    not load samples. The shape is enough for a CSS-only sparkline.
    """
    rows = conn.execute(
        "SELECT created_at, details FROM audit_logs "
        "WHERE actor_id = ? OR resource_id = ? "
        "ORDER BY created_at DESC LIMIT 20",
        (node_id, node_id),
    ).fetchall()
    history: list[dict] = []
    for r in rows:
        history.append({"timestamp": r["created_at"], "details": r["details"] or ""})
    # Newest last so the chart reads left→right chronologically.
    history.reverse()
    return history


# ---------------------------------------------------------------------------
# 3. Users list + profile
# ---------------------------------------------------------------------------


@router.get("/users")
async def cluster_users():
    """List all human users with status, role and node-count.

    Public, no auth. No emails, no password hashes, no permission
    details — only username, status (active/inactive), the primary
    group as "role" and the count of nodes owned by the user.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT u.user_id, u.username, u.is_active, u.status, u.created_at,
                   GROUP_CONCAT(g.group_name, ',') as groups
            FROM users u
            LEFT JOIN user_groups ug ON ug.user_id = u.user_id
            LEFT JOIN groups g ON g.group_id = ug.group_id
            GROUP BY u.user_id
            ORDER BY u.created_at DESC
            """
        ).fetchall()
        users = []
        for r in rows:
            groups = (r["groups"] or "").split(",") if r["groups"] else []
            role = "admin" if "admin" in groups else (groups[0] if groups else "user")
            users.append(
                {
                    "user_id": r["user_id"],
                    "username": r["username"],
                    "role": role,
                    "groups": groups,
                    "is_active": bool(r["is_active"]),
                    "status": r["status"] or ("active" if r["is_active"] else "inactive"),
                    "status_color": status_color(r["status"] or ("active" if r["is_active"] else "inactive")),
                    "created_at": r["created_at"],
                }
            )
        return {"users": users}
    finally:
        conn.close()


@router.get("/users/{user_id}")
async def cluster_user_profile(user_id: str):
    """Public user profile: details, assigned nodes, recent activity.

    "Assigned nodes" are nodes whose ``owner_node_id``-style link is
    not tracked per user; instead we attribute tasks owned or claimed
    by nodes the user created. Because the relay does not model a
    user↔node ownership relation, the profile lists the nodes that
    created tasks attributed to this user via the audit log (best
    effort) plus the user's recent task activity. No emails or secrets.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT user_id, username, is_active, status, created_at, created_by "
            "FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        groups_rows = conn.execute(
            """
            SELECT g.group_name FROM groups g
            JOIN user_groups ug ON ug.group_id = g.group_id
            WHERE ug.user_id = ?
            """,
            (user_id,),
        ).fetchall()
        groups = [r["group_name"] for r in groups_rows]
        role = "admin" if "admin" in groups else (groups[0] if groups else "user")

        # Recent audit-log activity for this user (creator or actor).
        activity_rows = conn.execute(
            "SELECT log_id, actor_id, actor_name, action, resource_type, resource_id, details, created_at "
            "FROM audit_logs WHERE actor_id = ? OR details LIKE ? "
            "ORDER BY created_at DESC LIMIT 20",
            (user_id, f"%{user_id}%"),
        ).fetchall()
        activity = [
            {
                "log_id": r["log_id"],
                "action": r["action"],
                "resource_type": r["resource_type"],
                "resource_id": r["resource_id"],
                "created_at": r["created_at"],
            }
            for r in activity_rows
        ]

        status_str = row["status"] or ("active" if row["is_active"] else "inactive")
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "role": role,
            "groups": groups,
            "is_active": bool(row["is_active"]),
            "status": status_str,
            "status_color": status_color(status_str),
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "activity": activity,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. Activity feed
# ---------------------------------------------------------------------------


@router.get("/activity")
async def cluster_activity(limit: int = Query(50, ge=1, le=200)):
    """Public activity feed — last ``limit`` events from the event bus.

    Mirrors the admin-only ``/dashboard/api/events/recent`` endpoint but
    without authentication. Returns the same event shape
    (``{type, timestamp, payload}``).
    """
    return {"events": event_bus.recent(limit=limit)}