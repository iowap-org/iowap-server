"""Dashboard router for the relay service — static UI + API endpoints."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from relay_server.api.v2.security import require_admin
from relay_server.core.db import get_conn
from relay_server.models import AuthContext

router = APIRouter()

STATIC_DIR = Path(__file__).parent.parent.parent / "static"


@router.get("/")
async def dashboard_index():
    """Serve the main dashboard HTML from a static file."""
    return FileResponse(STATIC_DIR / "dashboard.html")


@router.get("/api/overview")
async def dashboard_overview(ctx: AuthContext = Depends(require_admin)):
    """Aggregated cluster overview for the dashboard."""
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc)

        node_rows = conn.execute(
            "SELECT node_id, node_name, endpoint, capabilities, status, role, last_seen, first_heartbeat_seen, load, queue_depth "
            "FROM nodes ORDER BY registered_at DESC"
        ).fetchall()
        nodes = []
        online_count = 0
        for r in node_rows:
            cap_list = _safe_json(r["capabilities"], [])
            nodes.append(
                {
                    "node_id": r["node_id"],
                    "node_name": r["node_name"],
                    "endpoint": r["endpoint"],
                    "capabilities": cap_list,
                    "capability_names": [c.get("name") for c in cap_list],
                    "status": r["status"],
                    "role": r["role"],
                    "last_seen": r["last_seen"],
                    "first_heartbeat_seen": r["first_heartbeat_seen"],
                    "load": r["load"],
                    "queue_depth": r["queue_depth"],
                    "online": r["status"] == "online",
                }
            )
            if r["status"] == "online":
                online_count += 1

        task_rows = conn.execute(
            "SELECT task_id, task_name, status, priority, created_at, completed_at "
            "FROM tasks ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        tasks = [
            {
                "task_id": r["task_id"],
                "task_name": r["task_name"],
                "status": r["status"],
                "priority": r["priority"],
                "created_at": r["created_at"],
                "completed_at": r["completed_at"],
            }
            for r in task_rows
        ]

        status_counts = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall()
        task_stats = {r["status"]: r["cnt"] for r in status_counts}

        stage_rows = conn.execute(
            "SELECT stage_id, task_id, stage_name, capability, status, claimed_by, claimed_at "
            "FROM task_stages WHERE status IN ('pending','claimed') ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        active_stages = [
            {
                "stage_id": r["stage_id"],
                "task_id": r["task_id"],
                "stage_name": r["stage_name"],
                "capability": r["capability"],
                "status": r["status"],
                "claimed_by": r["claimed_by"],
                "claimed_at": r["claimed_at"],
            }
            for r in stage_rows
        ]

        artifact_rows = conn.execute(
            "SELECT artifact_id, task_id, stage_id, name, mime_type, size_bytes, created_at "
            "FROM artifacts ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        artifacts = [
            {
                "artifact_id": r["artifact_id"],
                "task_id": r["task_id"],
                "stage_id": r["stage_id"],
                "filename": r["name"],
                "content_type": r["mime_type"],
                "size_bytes": r["size_bytes"],
                "created_at": r["created_at"],
            }
            for r in artifact_rows
        ]

        return {
            "generated_at": now.isoformat(),
            "summary": {
                "total_nodes": len(nodes),
                "online_nodes": online_count,
                "total_tasks": sum(task_stats.values()),
                "task_stats": task_stats,
                "active_stages": len(active_stages),
                "total_artifacts": len(artifact_rows),
            },
            "nodes": nodes,
            "tasks": tasks,
            "active_stages": active_stages,
            "artifacts": artifacts,
        }
    finally:
        conn.close()


@router.get("/api/endpoints")
async def dashboard_endpoints(ctx: AuthContext = Depends(require_admin)):
    """Return the list of exposed v2 API endpoints."""
    return {"endpoints": _ENDPOINTS}


@router.get("/api/events/recent")
async def dashboard_recent_events(
    limit: int = Query(50, ge=1, le=200),
    ctx: AuthContext = Depends(require_admin),
):
    """Return recent events from the in-memory event log."""
    from relay_server.core.events import event_bus

    return {"events": event_bus.recent(limit=limit)}


def _safe_json(value: Any, default: Any):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


_ENDPOINTS = [
    {
        "method": "GET",
        "path": "/health",
        "auth": "none",
        "description": "Health check and basic status",
    },
    {
        "method": "POST",
        "path": "/relay/v2/auth/init-master",
        "auth": "none",
        "description": "Create the one-time master admin seed",
    },
    {
        "method": "POST",
        "path": "/relay/v2/auth/register",
        "auth": "none",
        "description": "Register an admin (with bootstrap secret) or pending service node",
    },
    {
        "method": "POST",
        "path": "/relay/v2/auth/refresh",
        "auth": "bearer",
        "description": "Refresh runtime token",
    },
    {
        "method": "GET",
        "path": "/relay/v2/admin/nodes",
        "auth": "admin",
        "description": "List all registered nodes",
    },
    {
        "method": "POST",
        "path": "/relay/v2/admin/nodes/{node_id}/approve",
        "auth": "admin",
        "description": "Approve a pending node",
    },
    {
        "method": "GET",
        "path": "/relay/v2/discovery/nodes",
        "auth": "bearer",
        "description": "List nodes with optional status/capability filters",
    },
    {
        "method": "GET",
        "path": "/relay/v2/discovery/capabilities",
        "auth": "bearer",
        "description": "Query available capabilities",
    },
    {
        "method": "POST",
        "path": "/relay/v2/discovery/heartbeat",
        "auth": "bearer",
        "description": "Send heartbeat from a node",
    },
    {
        "method": "GET",
        "path": "/relay/v2/presence/list",
        "auth": "bearer",
        "description": "List presence records",
    },
    {
        "method": "GET",
        "path": "/relay/v2/presence/{node_id}",
        "auth": "bearer",
        "description": "Get presence for a node",
    },
    {
        "method": "POST",
        "path": "/relay/v2/presence/update",
        "auth": "bearer",
        "description": "Update presence",
    },
    {
        "method": "POST",
        "path": "/relay/v2/scheduler/tasks",
        "auth": "bearer",
        "description": "Submit a new task",
    },
    {
        "method": "GET",
        "path": "/relay/v2/scheduler/tasks",
        "auth": "bearer",
        "description": "List tasks",
    },
    {
        "method": "GET",
        "path": "/relay/v2/scheduler/tasks/{task_id}",
        "auth": "bearer",
        "description": "Get task details",
    },
    {
        "method": "POST",
        "path": "/relay/v2/scheduler/claim",
        "auth": "bearer",
        "description": "Claim next available stage",
    },
    {
        "method": "POST",
        "path": "/relay/v2/scheduler/tasks/{task_id}/stages/{stage_id}/complete",
        "auth": "bearer",
        "description": "Complete a stage",
    },
    {
        "method": "POST",
        "path": "/relay/v2/artifacts/upload",
        "auth": "bearer",
        "description": "Upload an artifact",
    },
    {
        "method": "GET",
        "path": "/relay/v2/artifacts/{artifact_id}",
        "auth": "bearer",
        "description": "Download an artifact",
    },
    {
        "method": "GET",
        "path": "/relay/v2/events/stream",
        "auth": "bearer",
        "description": "Server-Sent Events stream",
    },
    {
        "method": "GET",
        "path": "/relay/v2/dashboard/",
        "auth": "admin",
        "description": "Web dashboard HTML",
    },
    {
        "method": "GET",
        "path": "/relay/v2/dashboard/api/overview",
        "auth": "admin",
        "description": "Dashboard overview JSON",
    },
    {
        "method": "GET",
        "path": "/relay/v2/dashboard/api/endpoints",
        "auth": "admin",
        "description": "API endpoint listing",
    },
    {
        "method": "GET",
        "path": "/relay/v2/dashboard/api/events/recent",
        "auth": "admin",
        "description": "Recent SSE events",
    },
]
