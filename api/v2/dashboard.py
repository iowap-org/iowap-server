"""Dashboard router for the relay service — static UI + API endpoints."""

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse

from relay_server.api.v2.security import require_admin
from relay_server.core.db import get_conn
from relay_server.models import AuthContext

router = APIRouter()

# Mount static dashboard assets at /dashboard/static when this router is included.
# The caller in v2/__init__.py wires StaticFiles separately.


@router.get("/", response_class=HTMLResponse)
async def dashboard_index():
    """Serve the main dashboard HTML."""
    return HTMLResponse(content=_DASHBOARD_HTML)


@router.get("/api/overview")
async def dashboard_overview(ctx: AuthContext = Depends(require_admin)):
    """Aggregated cluster overview for the dashboard."""
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc)

        # nodes
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

        # tasks
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

        # task status counts
        status_counts = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
        ).fetchall()
        task_stats = {r["status"]: r["cnt"] for r in status_counts}

        # stages needing work
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

        # artifacts
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


def _safe_json(value: Any, default: Any) -> Any:
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

_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI Relay Dashboard</title>
  <style>
    :root { --bg:#0f1115; --panel:#171a21; --text:#e6e6e6; --muted:#8892a0; --accent:#3b82f6; --ok:#22c55e; --warn:#f59e0b; --bad:#ef4444; }
    * { box-sizing: border-box; }
    body { margin:0; background:var(--bg); color:var(--text); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    header { padding: 1rem 1.5rem; border-bottom: 1px solid #2a2f3a; display:flex; align-items:center; justify-content:space-between; }
    h1 { margin:0; font-size:1.25rem; }
    .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap:1rem; padding:1rem 1.5rem; }
    .card { background:var(--panel); border:1px solid #2a2f3a; border-radius:.75rem; padding:1rem; }
    .card h2 { margin:0 0 .75rem; font-size:1rem; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
    .big { font-size:2rem; font-weight:700; }
    .ok { color:var(--ok); } .warn { color:var(--warn); } .bad { color:var(--bad); }
    table { width:100%; border-collapse:collapse; font-size:.85rem; }
    th, td { text-align:left; padding:.5rem; border-bottom:1px solid #2a2f3a; }
    th { color:var(--muted); font-weight:500; }
    .tag { display:inline-block; padding:.15rem .45rem; border-radius:.35rem; background:#263142; font-size:.75rem; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
    .refresh { padding:.5rem 1rem; background:var(--accent); color:white; border:none; border-radius:.5rem; cursor:pointer; }
    .refresh:disabled { opacity:.5; }
    .section { padding:0 1.5rem 1.5rem; }
    .section h2 { color:var(--muted); font-size:1rem; text-transform:uppercase; letter-spacing:.05em; margin:1.5rem 0 .75rem; }
    pre { background:#10131a; padding:.75rem; border-radius:.5rem; overflow:auto; max-height:300px; font-size:.8rem; }
    .error { color:var(--bad); }
  </style>
</head>
<body>
  <header>
    <h1>AI Relay Cluster Dashboard</h1>
    <div>
      <span id="status">loading...</span>
      <button class="refresh" id="btnRefresh" onclick="loadAll()">Refresh</button>
    </div>
  </header>

  <div class="grid" id="summary"></div>

  <div class="section">
    <h2>Nodes</h2>
    <div class="card"><table id="nodes"><thead><tr><th>ID</th><th>Name</th><th>Role</th><th>Status</th><th>Capabilities</th><th>Load</th><th>Queue</th><th>Last Seen</th></tr></thead><tbody></tbody></table></div>
  </div>

  <div class="section">
    <h2>Tasks</h2>
    <div class="card"><table id="tasks"><thead><tr><th>ID</th><th>Name</th><th>Status</th><th>Priority</th><th>Created</th></tr></thead><tbody></tbody></table></div>
  </div>

  <div class="section">
    <h2>Active Stages</h2>
    <div class="card"><table id="stages"><thead><tr><th>Stage</th><th>Task</th><th>Capability</th><th>Status</th><th>Claimed By</th></tr></thead><tbody></tbody></table></div>
  </div>

  <div class="section">
    <h2>Recent Events</h2>
    <div class="card"><pre id="events">loading...</pre></div>
  </div>

  <div class="section">
    <h2>API Endpoints</h2>
    <div class="card"><table id="endpoints"><thead><tr><th>Method</th><th>Path</th><th>Auth</th><th>Description</th></tr></thead><tbody></tbody></table></div>
  </div>

<script>
const token = new URLSearchParams(location.search).get('token') || localStorage.getItem('relay_token') || '';
if (token) localStorage.setItem('relay_token', token);

function fmt(d) {
  if (!d) return '-';
  const s = new Date(d).toLocaleString();
  return isNaN(new Date(d)) ? d : s;
}

async function fetchJson(path, opts={}) {
  const res = await fetch(path, { headers: { 'Authorization': 'Bearer ' + token, ...opts.headers }, ...opts });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function loadAll() {
  const btn = document.getElementById('btnRefresh');
  btn.disabled = true;
  document.getElementById('status').textContent = 'loading...';
  try {
    const [overview, endpoints, events] = await Promise.all([
      fetchJson('/relay/v2/dashboard/api/overview'),
      fetchJson('/relay/v2/dashboard/api/endpoints'),
      fetchJson('/relay/v2/dashboard/api/events/recent?limit=50'),
    ]);

    const s = overview.summary;
    document.getElementById('summary').innerHTML = `
      <div class="card"><h2>Nodes</h2><div class="big ${s.online_nodes>0?'ok':'bad'}">${s.online_nodes}/${s.total_nodes}</div><div>online</div></div>
      <div class="card"><h2>Tasks</h2><div class="big">${s.total_tasks}</div><div>${Object.entries(s.task_stats).map(([k,v])=>k+': '+v).join(' · ')}</div></div>
      <div class="card"><h2>Active Stages</h2><div class="big ${s.active_stages>0?'warn':'ok'}">${s.active_stages}</div></div>
      <div class="card"><h2>Artifacts</h2><div class="big">${s.total_artifacts}</div></div>
    `;

    document.querySelector('#nodes tbody').innerHTML = overview.nodes.map(n => `
      <tr>
        <td class="mono">${n.node_id}</td>
        <td>${n.node_name}</td>
        <td><span class="tag">${n.role}</span></td>
        <td><span class="tag ${n.status==='online'?'ok':'bad'}">${n.status}</span></td>
        <td>${(n.capability_names||[]).join(', ')}</td>
        <td>${n.load ?? '-'}</td>
        <td>${n.queue_depth ?? '-'}</td>
        <td>${fmt(n.last_seen)}</td>
      </tr>
    `).join('');

    document.querySelector('#tasks tbody').innerHTML = overview.tasks.map(t => `
      <tr>
        <td class="mono">${t.task_id}</td>
        <td>${t.task_name}</td>
        <td><span class="tag">${t.status}</span></td>
        <td>${t.priority}</td>
        <td>${fmt(t.created_at)}</td>
      </tr>
    `).join('');

    document.querySelector('#stages tbody').innerHTML = overview.active_stages.map(st => `
      <tr>
        <td class="mono">${st.stage_id}</td>
        <td class="mono">${st.task_id}</td>
        <td>${st.capability}</td>
        <td><span class="tag ${st.status==='claimed'?'warn':'ok'}">${st.status}</span></td>
        <td>${st.claimed_by || '-'}</td>
      </tr>
    `).join('');

    document.getElementById('events').textContent = events.events.map(e =>
      `[${fmt(e.timestamp)}] ${e.type} ${JSON.stringify(e.payload)}`
    ).join('\n') || 'no events yet';

    document.querySelector('#endpoints tbody').innerHTML = endpoints.endpoints.map(ep => `
      <tr>
        <td><span class="tag">${ep.method}</span></td>
        <td class="mono">${ep.path}</td>
        <td>${ep.auth}</td>
        <td>${ep.description}</td>
      </tr>
    `).join('');

    document.getElementById('status').textContent = 'updated ' + fmt(overview.generated_at);
  } catch (err) {
    document.getElementById('status').innerHTML = `<span class="error">error: ${err.message}</span>`;
    console.error(err);
  } finally {
    btn.disabled = false;
  }
}

if (!token) {
  document.body.innerHTML = '<div style="padding:2rem;"><h1>Token required</h1><p>Add ?token=YOUR_RUNTIME_TOKEN to the URL or set it in localStorage.</p></div>';
} else {
  loadAll();
  setInterval(loadAll, 10000);
}
</script>
</body>
</html>
"""
