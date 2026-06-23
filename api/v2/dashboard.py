import secrets
"""Dashboard router for the relay service — static UI + API endpoints."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import markdown
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from relay_server.api.v2.security import (
    check_dashboard_permission,
    require_dashboard_user,
)
from relay_server.config import settings
from relay_server.core.db import get_conn
from relay_server.core.session import (
    CSRF_MAX_AGE_SECONDS,
    SESSION_MAX_AGE_SECONDS,
    generate_csrf_token,
    sign_user_cookie,
)
from relay_server.core.users import (
    authenticate_master_seed,
    authenticate_user,
    change_user_password,
    create_user,
    delete_user,
    get_user_permissions,
    has_admin_user,
    list_groups,
    list_permissions,
    list_users,
    set_group_permissions,
    set_user_active,
    set_user_groups,
    set_user_password,
)
from relay_server.models import AuthContext

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

STATIC_DIR = Path(__file__).parent.parent.parent / "static"
DOCS_DIR = Path(__file__).parent.parent.parent.parent.parent / "docs"
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent
USER_COOKIE = "relay_user"
CSRF_COOKIE = "relay_csrf"
CSRF_HEADER = "x-csrf-token"

ALLOWED_DOCS = {
    "readme": PROJECT_ROOT / "README.md",
    "changelog": PROJECT_ROOT / "CHANGELOG.md",
    "agent-readme": PROJECT_ROOT / "AGENT_README.md",
    "node-readme": DOCS_DIR / "node-readme.md",
    "token-concept": DOCS_DIR / "token-concept.md",
    "dashboard": DOCS_DIR / "dashboard.md",
    "setup": DOCS_DIR / "setup.md",
    "nodes-design": DOCS_DIR / "nodes-design.md",
    "adr-001-node-id-schema": DOCS_DIR / "adr" / "adr-001-node-id-schema.md",
    "adr-002-bootstrap-and-recovery": DOCS_DIR / "adr" / "adr-002-bootstrap-and-recovery.md",
}


def _render_markdown(path: Path) -> str:
    md = path.read_text(encoding="utf-8")
    html = markdown.markdown(md, extensions=["fenced_code", "tables"])
    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
  <title>{path.stem} — AI Relay Docs</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: system-ui, -apple-system, BlinkMacSystemFont, sans-serif; margin: 0 auto; max-width: 800px; padding: 2rem 1rem; background: #0b0d11; color: #e0e2e8; line-height: 1.6; }}
    a {{ color: #7aa2ff; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    h1, h2, h3 {{ color: #fff; border-bottom: 1px solid #2a2f3a; padding-bottom: .25rem; }}
    code {{ background: #1a1d25; padding: .15rem .35rem; border-radius: .25rem; }}
    pre {{ background: #1a1d25; padding: 1rem; border-radius: .5rem; overflow-x: auto; }}
    pre code {{ background: transparent; padding: 0; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #2a2f3a; padding: .5rem; text-align: left; }}
    th {{ background: #1a1d25; }}
  </style>
</head>
<body>
  {html}
</body>
</html>
""".strip()


def _set_user_cookie(response, user: dict) -> None:
    response.set_cookie(
        key=USER_COOKIE,
        value=sign_user_cookie(user),
        httponly=True,
        max_age=SESSION_MAX_AGE_SECONDS,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )


def _set_csrf_cookie(response) -> None:
    response.set_cookie(
        key=CSRF_COOKIE,
        value=generate_csrf_token(),
        httponly=False,
        max_age=CSRF_MAX_AGE_SECONDS,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )


def _clear_cookies(response) -> None:
    response.delete_cookie(
        key=USER_COOKIE,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    response.delete_cookie(
        key=CSRF_COOKIE,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )


def _verify_csrf(request: Request) -> None:
    cookie_token = request.cookies.get(CSRF_COOKIE)
    header_token = request.headers.get(CSRF_HEADER)
    if not cookie_token or not header_token or cookie_token != header_token:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF token mismatch",
        )


@router.get("/")
async def dashboard_index(request: Request, ctx: AuthContext = Depends(require_dashboard_user)):
    """Serve the main dashboard HTML from a static file."""
    check_dashboard_permission(ctx, "dashboard:view")
    return FileResponse(STATIC_DIR / "dashboard.html")


@router.get("/login")
async def dashboard_login_page() -> FileResponse:
    """Serve the login page and set a CSRF cookie for form submissions."""
    response = FileResponse(STATIC_DIR / "login.html")
    _set_csrf_cookie(response)
    return response


@router.get("/agent-readme", include_in_schema=False)
async def dashboard_agent_readme():
    """Redirect to the rendered node-readme document."""
    return RedirectResponse("/relay/v2/dashboard/docs/node-readme", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/docs/{doc_name}", include_in_schema=False)
async def dashboard_doc(doc_name: str):
    """Render a public Markdown document as HTML.

    The whitelist maps short names to files inside the repository. Unknown
    names return 404.
    """
    path = ALLOWED_DOCS.get(doc_name)
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    content = _render_markdown(path)
    return HTMLResponse(content=content)


@router.get("/docs", include_in_schema=False)
async def dashboard_doc_index():
    """List public documents."""
    items = []
    for name, path in ALLOWED_DOCS.items():
        items.append({
            "name": name,
            "title": path.stem,
            "url": f"/relay/v2/dashboard/docs/{name}",
            "available": path.exists(),
        })
    return JSONResponse({"docs": items})


@router.post("/login")
@limiter.limit("5/minute")
async def dashboard_login(
    request: Request,
    mode: str = Form("user"),
    username: str = Form(""),
    password: str = Form(""),
    seed: str = Form(""),
):
    """Validate username/password or master admin seed and set session cookie.

    Master-seed login is only available while no human admin user exists.
    Once a human admin has been created, seed login is disabled to avoid
    leaking the long-lived master seed through the browser.
    """
    if mode == "seed":
        if has_admin_user() and not settings.enable_master_seed_login:
            return RedirectResponse(
                url="/relay/v2/dashboard/login?error=Seed%20login%20disabled",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        user = authenticate_master_seed(seed)
        if not user:
            return RedirectResponse(
                url="/relay/v2/dashboard/login?error=Invalid%20master%20seed",
                status_code=status.HTTP_303_SEE_OTHER,
            )
    else:
        user = authenticate_user(username, password)
        if not user:
            return RedirectResponse(
                url="/relay/v2/dashboard/login?error=Invalid%20credentials",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        if user.get("force_password_change"):
            # Issue a short-lived signed token that only allows password change.
            response = RedirectResponse(
                url="/relay/v2/dashboard/change-password",
                status_code=status.HTTP_303_SEE_OTHER,
            )
            _set_user_cookie(response, user)
            _set_csrf_cookie(response)
            return response

    if mode == "seed":
        response = RedirectResponse(
            url="/relay/v2/dashboard/bootstrap", status_code=status.HTTP_303_SEE_OTHER
        )
    else:
        response = RedirectResponse(url="/relay/v2/dashboard/", status_code=status.HTTP_303_SEE_OTHER)
    _set_user_cookie(response, user)
    _set_csrf_cookie(response)
    return response


@router.get("/change-password")
async def dashboard_change_password_page(request: Request):
    """Page where users with force_password_change must set a new password."""
    response = FileResponse(STATIC_DIR / "change-password.html")
    _set_csrf_cookie(response)
    return response


@router.get("/bootstrap")
async def dashboard_bootstrap_page(request: Request, ctx: AuthContext = Depends(require_dashboard_user)):
    """Page shown after master-seed login to create the first human admin."""
    if ctx.user_id != "__master__" and not settings.enable_master_seed_login:
        return RedirectResponse("/relay/v2/dashboard/", status_code=status.HTTP_303_SEE_OTHER)
    return FileResponse(STATIC_DIR / "bootstrap.html")


@router.post("/api/bootstrap")
async def dashboard_bootstrap_create_admin(
    request: Request,
    username: str = Form(...),
    email: str = Form(""),
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """Create the first human admin while logged in via master seed."""
    _verify_csrf(request)
    if ctx.user_id != "__master__":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the master seed session can create the first admin.",
        )
    if has_admin_user() and not settings.enable_master_seed_login:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A human admin already exists.",
        )
    temp_password = secrets.token_urlsafe(16)
    user = create_user(
        username=username,
        password=temp_password,
        group_names=["admin"],
        email=email or None,
        created_by="master_seed",
        force_password_change=True,
    )
    return {"status": "ok", "user_id": user["user_id"], "temporary_password": temp_password}


@router.post("/logout")
async def dashboard_logout(request: Request, ctx: AuthContext = Depends(require_dashboard_user)):
    """Clear the dashboard session cookie."""
    _verify_csrf(request)
    response = RedirectResponse(
        url="/relay/v2/dashboard/login", status_code=status.HTTP_303_SEE_OTHER
    )
    _clear_cookies(response)
    return response


@router.get("/api/me")
async def dashboard_me(request: Request, ctx: AuthContext = Depends(require_dashboard_user)):
    """Return current dashboard user info."""
    if ctx.user_id == "__master__":
        permissions = [p["permission_name"] for p in list_permissions()]
    elif ctx.user_id:
        permissions = get_user_permissions(ctx.user_id)
    else:
        permissions = []
    return {
        "user_id": ctx.user_id,
        "username": ctx.username,
        "role": ctx.role,
        "is_master": ctx.user_id == "__master__",
        "permissions": permissions,
    }


@router.get("/api/overview")
async def dashboard_overview(request: Request, ctx: AuthContext = Depends(require_dashboard_user)):
    """Aggregated cluster overview for the dashboard."""
    check_dashboard_permission(ctx, "dashboard:view")
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc)
        DASHBOARD_ADMIN_NODE = "__dashboard_admin__"
        node_rows = conn.execute(
            "SELECT node_id, node_name, endpoint, capabilities, status, role, last_seen, first_heartbeat_seen, load, queue_depth "
            "FROM nodes ORDER BY registered_at DESC"
        ).fetchall()
        nodes = []
        online_count = 0
        for r in node_rows:
            if r["node_id"] == DASHBOARD_ADMIN_NODE:
                continue
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
async def dashboard_endpoints(request: Request, ctx: AuthContext = Depends(require_dashboard_user)):
    """Return the list of exposed v2 API endpoints."""
    check_dashboard_permission(ctx, "dashboard:view")
    return {"endpoints": _ENDPOINTS}


@router.get("/api/events/recent")
async def dashboard_recent_events(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """Return recent events from the in-memory event log."""
    check_dashboard_permission(ctx, "dashboard:view")
    from relay_server.core.events import event_bus

    return {"events": event_bus.recent(limit=limit)}


# --- RBAC MANAGEMENT ---


@router.get("/api/users")
async def dashboard_list_users(
    request: Request,
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """List human users."""
    check_dashboard_permission(ctx, "users:manage")
    return {"users": list_users()}


@router.post("/api/users")
async def dashboard_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    email: str = Form(""),
    groups: str = Form("user"),
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """Create a new human user. Requires users:manage permission."""
    _verify_csrf(request)
    check_dashboard_permission(ctx, "users:manage")
    group_list = [g.strip() for g in groups.split(",") if g.strip()]
    created_by = ctx.username or ctx.node_id
    user = create_user(
        username=username,
        password=password,
        group_names=group_list,
        email=email or None,
        created_by=created_by,
    )
    return user


@router.post("/api/users/{user_id}/groups")
async def dashboard_set_user_groups(
    request: Request,
    user_id: str,
    groups: str = Form(...),
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """Set groups for a user."""
    _verify_csrf(request)
    check_dashboard_permission(ctx, "users:manage")
    group_list = [g.strip() for g in groups.split(",") if g.strip()]
    set_user_groups(user_id, group_list)
    return {"status": "ok"}


@router.post("/api/me/password")
async def dashboard_change_own_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """Change the current user's own password and refresh the session cookie."""
    _verify_csrf(request)
    change_user_password(ctx.user_id, current_password, new_password)
    # Refresh the user cookie so the middleware no longer sees force_password_change=True.
    user = {"user_id": ctx.user_id, "username": ctx.username, "groups": ctx.groups}
    response = JSONResponse({"status": "ok", "redirect_url": "/relay/v2/dashboard/"})
    _set_user_cookie(response, user)
    _set_csrf_cookie(response)
    return response


@router.post("/api/users/{user_id}/password")
async def dashboard_set_user_password(
    request: Request,
    user_id: str,
    password: str = Form(...),
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """Reset a user's password."""
    _verify_csrf(request)
    check_dashboard_permission(ctx, "users:manage")
    set_user_password(user_id, password)
    return {"status": "ok"}


@router.post("/api/users/{user_id}/active")
async def dashboard_set_user_active(
    request: Request,
    user_id: str,
    active: bool = Form(...),
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """Activate or deactivate a user."""
    _verify_csrf(request)
    check_dashboard_permission(ctx, "users:manage")
    set_user_active(user_id, active)
    return {"status": "ok"}


@router.delete("/api/users/{user_id}")
async def dashboard_delete_user(
    request: Request,
    user_id: str,
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """Delete a human user."""
    _verify_csrf(request)
    check_dashboard_permission(ctx, "users:manage")
    delete_user(user_id)
    return {"status": "deleted", "user_id": user_id}


@router.get("/api/groups")
async def dashboard_list_groups(
    request: Request,
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """List groups and their permissions."""
    check_dashboard_permission(ctx, "groups:manage")
    return {"groups": list_groups()}


@router.get("/api/permissions")
async def dashboard_list_permissions(
    request: Request,
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """List all available permissions."""
    check_dashboard_permission(ctx, "groups:manage")
    return {"permissions": list_permissions()}


@router.post("/api/groups/{group_id}/permissions")
async def dashboard_set_group_permissions(
    request: Request,
    group_id: str,
    permissions: str = Form(...),
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """Set permissions for a group."""
    _verify_csrf(request)
    check_dashboard_permission(ctx, "groups:manage")
    perm_list = [p.strip() for p in permissions.split(",") if p.strip()]
    set_group_permissions(group_id, perm_list)
    return {"status": "ok"}


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
        "path": "/relay/v2/auth/register",
        "auth": "none",
        "description": "Register a worker/service node in pending state",
    },
    {
        "method": "POST",
        "path": "/relay/v2/auth/register-admin",
        "auth": "bootstrap secret",
        "description": "Register an admin node using the master admin seed",
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
        "auth": "dashboard",
        "description": "List all registered nodes (requires dashboard:view)",
    },
    {
        "method": "POST",
        "path": "/relay/v2/admin/nodes/{node_id}/approve",
        "auth": "dashboard",
        "description": "Approve a pending node (requires nodes:approve)",
    },
    {
        "method": "POST",
        "path": "/relay/v2/admin/nodes/{node_id}/token",
        "auth": "dashboard",
        "description": "Issue a new runtime token for an approved/offline node (requires nodes:token)",
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
        "path": "/relay/v2/dashboard/login",
        "auth": "none",
        "description": "Dashboard login page",
    },
    {
        "method": "POST",
        "path": "/relay/v2/dashboard/login",
        "auth": "none",
        "description": "Dashboard login endpoint",
    },
    {
        "method": "POST",
        "path": "/relay/v2/dashboard/logout",
        "auth": "admin",
        "description": "Dashboard logout endpoint",
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
