"""Dynamic node route proxy — T-075.

Nodes declare API routes in their capability YAML. The relay stores them
in the ``node_routes`` table on each heartbeat. The dashboard router
catches requests under ``/relay/v2/dashboard/api/node-routes/``, looks up
the matching route in the DB, checks the required auth mode, and proxies
the request to the node's upstream URL.

Auth modes:
  - ``session`` — requires a valid dashboard session cookie
  - ``node_token`` — requires a valid Bearer node token
  - ``none`` — no authentication (public endpoint)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from relay_server.api.v2.security import (
    check_dashboard_permission,
    get_auth_context,
    require_dashboard_user,
)
from relay_server.core.db import get_conn, q
from relay_server.models import AuthContext

logger = logging.getLogger(__name__)

router = APIRouter()

NODE_ROUTES_PREFIX = "/relay/v2/dashboard/api/node-routes"


@router.api_route("/api/node-routes/{node_id}/{rest_of_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_node_route(
    node_id: str,
    rest_of_path: str,
    request: Request,
    ctx: AuthContext = Depends(require_dashboard_user),
):
    """Proxy a request to a dynamic node route.

    The path after ``/api/node-routes/{node_id}/`` is the sub-path declared
    by the node in its capability YAML. The relay looks up the route in
    the ``node_routes`` table, checks auth, and proxies.
    """
    # Build the sub-path: /rest/of/path
    sub_path = "/" + rest_of_path if rest_of_path else "/"

    # Look up the route in the DB.
    route = _lookup_route(node_id, sub_path, request.method)
    if route is None:
        raise HTTPException(status_code=404, detail="Node route not found")

    # Check auth.
    auth = route["auth"]
    if auth == "session":
        check_dashboard_permission(ctx, "dashboard:view")
    elif auth == "node_token":
        # Re-authenticate with Bearer token instead of session cookie.
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        await get_auth_context(auth_header)
    # auth == "none" — no check needed

    # Proxy the request to the upstream.
    upstream = route["upstream"]
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            body = await request.body()
            resp = await client.request(
                method=request.method,
                url=upstream,
                headers=_forward_headers(request),
                content=body,
            )
            return Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type"),
            )
    except httpx.RequestError as e:
        logger.warning("Node route proxy error: %s %s -> %s: %s", request.method, request.url.path, upstream, e)
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")


def _lookup_route(node_id: str, path: str, method: str) -> dict[str, Any] | None:
    """Look up a route in the node_routes table."""
    conn = get_conn()
    try:
        row = conn.execute(
            q("SELECT node_id, path, method, auth, upstream, description "
            "FROM node_routes WHERE node_id = ? AND path = ? AND method = ?", (node_id, path, method.upper())),
        ).fetchone()
        if row is None:
            return None
        return dict(row)
    finally:
        conn.close()


def _forward_headers(request: Request) -> dict[str, str]:
    """Forward headers, stripping hop-by-hop headers."""
    headers = dict(request.headers)
    for h in ("host", "content-length", "transfer-encoding", "connection"):
        headers.pop(h, None)
    return headers
