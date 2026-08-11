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

T-123: a route may be *temporary* (``expires_at`` set). Temp routes are
registered via ``POST /api/node-routes/register`` (T-124) with a TTL and
expire automatically; an expired temp route is treated as 404 by
:func:`_lookup_route` so the proxy never forwards to a dead upstream.
Permanent heartbeat routes have ``expires_at IS NULL`` and never expire.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from relay_server.api.v2.security import (
    check_dashboard_permission,
    extract_bearer,
    get_auth_context,
    require_dashboard_user,
)
from relay_server.config import settings
from relay_server.core.db import get_conn, q
from relay_server.models import AuthContext

logger = logging.getLogger(__name__)

router = APIRouter()

NODE_ROUTES_PREFIX = "/relay/v2/dashboard/api/node-routes"


@router.api_route(
    "/api/node-routes/{node_id}/{rest_of_path:path}", methods=["GET", "POST", "PUT", "PATCH"]
)
async def proxy_node_route(
    node_id: str,
    rest_of_path: str,
    request: Request,
):
    """Proxy a request to a dynamic node route.

    The path after ``/api/node-routes/{node_id}/`` is the sub-path declared
    by the node in its capability YAML. The relay looks up the route in
    the ``node_routes`` table, checks auth, and proxies.

    T-123/124: the matched route's ``auth`` mode decides how the *caller*
    is authenticated. A permanent ``session`` route requires a dashboard
    session cookie (validated here without a FastAPI dependency so the
    lookup can happen first). A temporary ``node_token`` bridge route is
    reached by *another node* presenting a Bearer token and must work
    without a dashboard session cookie. ``none`` is public.

    ``DELETE`` is **not** proxied — it is reserved for the unregister
    endpoint (:func:`delete_node_route`) so a route owner can revoke a
    (temp) route before its TTL lapses. A node that needs to expose a
    DELETE-capable upstream route must register it under a non-DELETE
    method (the storage node uses POST for uploads/downloads).
    """
    # Build the sub-path: /rest/of/path
    sub_path = "/" + rest_of_path if rest_of_path else "/"

    # Look up the route in the DB first so the auth mode is known before
    # we reject the caller. (Bridge routes have no dashboard session.)
    route = _lookup_route(node_id, sub_path, request.method)
    if route is None:
        raise HTTPException(status_code=404, detail="Node route not found")

    # Check auth based on the matched route's declared auth mode.
    auth = route["auth"]
    if auth == "session":
        ctx = await _dashboard_ctx(request)
        check_dashboard_permission(ctx, "dashboard:view")
    elif auth == "node_token":
        # T-123/124: bridge routes authenticate the *caller* (a node)
        # via a Bearer token, not a dashboard session cookie. A node
        # reaching a bridge route has no dashboard session — validate
        # the Bearer token directly.
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Bearer token required")
        await get_auth_context(auth_header)
    # auth == "none" — no check needed

    # Proxy the request to the upstream.
    upstream = route["upstream"]
    try:
        # T-129: stream the request body and the upstream response chunkwise
        # so large files (bridge upload/download) never sit fully in RAM.
        # ``request.stream()`` yields the body in chunks; ``client.send``
        # with ``stream=True`` keeps the upstream response lazy so we can
        # forward it via ``StreamingResponse`` instead of ``resp.content``.
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)) as client:
            # T-157: only stream a request body for methods that carry one.
            # `request.stream()` on a GET/HEAD yields an empty ASGI receive
            # generator that httpx cannot iterate — it aborts the upstream
            # response read with httpx.ReadError, so the caller sees a
            # Content-Length header but an empty body ("Response content
            # shorter than Content-Length"). Pass content only for
            # body-capable methods; GET/HEAD get none.
            kwargs = {}
            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                kwargs["content"] = request.stream()
            req = client.build_request(
                method=request.method,
                url=upstream,
                headers=_forward_headers(request),
                **kwargs,
            )
            resp = await client.send(req, stream=True)

            # Consume the upstream response lazily: forward status, headers
            # and the chunked body to the caller without buffering it.
            async def _upstream_bytes():
                try:
                    async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                        yield chunk
                finally:
                    await resp.aclose()

            return StreamingResponse(
                _upstream_bytes(),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type"),
                headers={
                    k: v
                    for k, v in resp.headers.items()
                    if k.lower() not in ("content-encoding", "transfer-encoding", "connection")
                },
            )
    except httpx.RequestError as e:
        logger.warning(
            "Node route proxy error: %s %s -> %s: %s", request.method, request.url.path, upstream, e
        )
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")


async def _dashboard_ctx(request: Request) -> AuthContext:
    """Resolve a dashboard session from the request cookie (T-123 refactor).

    The proxy previously used ``Depends(require_dashboard_user)``. That
    runs *before* the route lookup and would 401 a node reaching a
    ``node_token`` bridge route (which has no session cookie). The lookup
    now happens first; for ``session`` routes we resolve the session here
    using the same cookie + security helper.
    """
    cookie = request.cookies.get("relay_user")
    return await require_dashboard_user(
        authorization=request.headers.get("authorization"), relay_user=cookie
    )


def _lookup_route(node_id: str, path: str, method: str) -> dict[str, Any] | None:
    """Look up a route in the node_routes table.

    T-123: a temporary route (``expires_at`` set) that has already
    expired is treated as "not found" so the proxy returns 404 instead
    of forwarding to a dead upstream. Permanent routes
    (``expires_at IS NULL``) never expire.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            q(
                "SELECT node_id, path, method, auth, upstream, description, expires_at "
                "FROM node_routes WHERE node_id = ? AND path = ? AND method = ?",
                (node_id, path, method.upper()),
            ),
        ).fetchone()
        if row is None:
            return None
        # T-123: expired temp route → behave as 404.
        if row["expires_at"] and _expired(row["expires_at"]):
            return None
        return dict(row)
    finally:
        conn.close()


def _expired(expires_at: str) -> bool:
    """Return True when an ISO-8601 ``expires_at`` timestamp is in the past.

    Malformed timestamps are treated as expired (fail-closed) so a bad
    value never keeps a temp route alive forever.
    """
    try:
        exp = datetime.fromisoformat(expires_at)
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True
    return exp <= datetime.now(timezone.utc)


def _forward_headers(request: Request) -> dict[str, str]:
    """Forward headers, stripping hop-by-hop headers.

    T-129: ``content-length`` is **kept** so a streaming request body
    (``request.stream()``) lets the upstream know the body size. The
    hop-by-hop headers (host, transfer-encoding, connection) are still
    stripped per RFC 7230.
    """
    headers = dict(request.headers)
    for h in ("host", "transfer-encoding", "connection"):
        headers.pop(h, None)
    return headers


# ---------------------------------------------------------------------------
# T-124: task-driven temp route registration + deletion.
# ---------------------------------------------------------------------------

# Path prefixes under which a node may register a temp bridge route. The
# storage node uses ``/upload/`` and ``/download/`` channels for large
# file handoff; we keep the allowlist small so a compromised node cannot
# shadow arbitrary dashboard routes with temp routes.
_TEMP_ROUTE_PREFIXES = ("/upload/", "/download/")


def _normalize_path(path: str) -> str:
    """Normalize a temp-route path: leading slash, no trailing slash."""
    p = path.strip()
    if not p.startswith("/"):
        p = "/" + p
    # Keep a single trailing slash off (except for the root "/").
    if len(p) > 1 and p.endswith("/"):
        p = p.rstrip("/") or "/"
    return p


def _allowed_temp_path(path: str) -> bool:
    """Return True when ``path`` is under one of the allowed prefixes."""
    return any(
        path.startswith(prefix) or path == prefix.rstrip("/") for prefix in _TEMP_ROUTE_PREFIXES
    )


@router.get("/api/node-routes")
async def list_own_routes(
    request: Request,
):
    """List all routes owned by the calling node (T-136).

    Auth: Bearer node token. Returns every ``node_routes`` row for the
    caller's ``node_id`` (resolved from the token), including permanent
    heartbeat routes (``expires_at IS NULL``) and live temp routes. The
    caller is a node that wants to inspect/manage its own temp routes
    via ``node-cli route list``; it cannot list another node's routes.
    """
    auth_header = request.headers.get("authorization", "")
    token = extract_bearer(auth_header)
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    ctx = await get_auth_context(auth_header)

    conn = get_conn()
    try:
        rows = conn.execute(
            q(
                "SELECT node_id, path, method, auth, upstream, description, "
                "expires_at, channel_id FROM node_routes WHERE node_id = ? "
                "ORDER BY expires_at IS NOT NULL, expires_at, path",
                (ctx.node_id,),
            ),
        ).fetchall()
    finally:
        conn.close()

    return {
        "status": "ok",
        "node_id": ctx.node_id,
        "routes": [dict(r) for r in rows],
    }


@router.post("/api/node-routes/register")
async def register_temp_route(
    request: Request,
):
    """Register a temporary bridge route owned by the calling node (T-124).

    Auth: Bearer node token (rt-...). The node registering the route is
    the route's owner — the ``node_id`` is taken from the token, not the
    body, so a node cannot register routes on behalf of another node.

    Body::

        {
          "path": "/upload/abc123",
          "method": "POST",
          "ttl_seconds": 3600,
          "upstream": "http://storage-node:8791/upload/abc123",
          "channel_id": "ch_abc123",
          "description": "optional human note"
        }

    The route is stored with ``auth = "node_token"``, an
    ``expires_at = now + ttl_seconds`` and the given ``channel_id``. A
    later heartbeat from the same node does NOT replace the route
    (``_sync_node_routes`` only replaces permanent routes with
    ``expires_at IS NULL``). The route is reaped by ``temp_route_cleanup``
    (T-125) after it expires; the owner can also DELETE it early.
    """
    auth_header = request.headers.get("authorization", "")
    token = extract_bearer(auth_header)
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    ctx = await get_auth_context(auth_header)

    body = await request.json()
    raw_path = str(body.get("path", "")).strip()
    method = str(body.get("method", "")).strip().upper()
    upstream = str(body.get("upstream", "")).strip()
    channel_id = str(body.get("channel_id", "")).strip()
    ttl_seconds = body.get("ttl_seconds")
    description = str(body.get("description", "") or "").strip()

    # --- validation -------------------------------------------------------
    if not raw_path:
        raise HTTPException(status_code=400, detail="path is required")
    if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
        raise HTTPException(status_code=400, detail=f"unsupported method: {method!r}")
    if not upstream:
        raise HTTPException(status_code=400, detail="upstream is required")
    if not channel_id:
        raise HTTPException(status_code=400, detail="channel_id is required for temp routes")
    if len(channel_id) > 64:
        raise HTTPException(status_code=400, detail="channel_id too long (max 64)")
    if len(upstream) > 2048:
        raise HTTPException(status_code=400, detail="upstream too long (max 2048)")
    # TTL bounds — must be a positive integer and capped by the server.
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="ttl_seconds must be a positive integer")
    if ttl <= 0:
        raise HTTPException(status_code=400, detail="ttl_seconds must be > 0")
    max_ttl = settings.temp_route_max_ttl_seconds
    if ttl > max_ttl:
        raise HTTPException(
            status_code=400,
            detail=f"ttl_seconds exceeds maximum ({max_ttl})",
        )
    path = _normalize_path(raw_path)
    if not _allowed_temp_path(path):
        raise HTTPException(
            status_code=400,
            detail=f"temp route path must start with one of {list(_TEMP_ROUTE_PREFIXES)}",
        )

    # --- persist ----------------------------------------------------------
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(seconds=ttl)).isoformat()
    conn = get_conn()
    try:
        # UPSERT on (node_id, path, method): re-registering the same path
        # (e.g. channel resumed) refreshes the TTL instead of clashing.
        conn.execute(
            q(
                """
            INSERT INTO node_routes
                (node_id, path, method, auth, upstream, description,
                 expires_at, channel_id)
            VALUES (?, ?, ?, 'node_token', ?, ?, ?, ?)
            ON CONFLICT(node_id, path, method) DO UPDATE SET
                auth = 'node_token',
                upstream = excluded.upstream,
                description = excluded.description,
                expires_at = excluded.expires_at,
                channel_id = excluded.channel_id
            """,
                (ctx.node_id, path, method, upstream, description, expires_at, channel_id),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "temp route registered: node=%s path=%s method=%s channel=%s ttl=%ss",
        ctx.node_id,
        path,
        method,
        channel_id,
        ttl,
    )
    return {
        "status": "ok",
        "node_id": ctx.node_id,
        "path": path,
        "method": method,
        "expires_at": expires_at,
        "channel_id": channel_id,
    }


@router.delete("/api/node-routes/{node_id}/{rest_of_path:path}")
async def delete_node_route(
    node_id: str,
    rest_of_path: str,
    request: Request,
):
    """Delete a route owned by ``node_id`` (T-124 unregister path).

    Auth: Bearer node token. The caller must own the route — i.e. the
    token's ``node_id`` must equal the path's ``{node_id}``. A node can
    therefore revoke its own temp route before the TTL expires. The
    ``method`` is taken from the query string (default ``GET``).
    """
    auth_header = request.headers.get("authorization", "")
    token = extract_bearer(auth_header)
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    ctx = await get_auth_context(auth_header)

    if ctx.node_id != node_id:
        raise HTTPException(status_code=403, detail="cannot delete a route owned by another node")

    method = (request.query_params.get("method") or "GET").strip().upper()
    path = "/" + rest_of_path if rest_of_path else "/"

    conn = get_conn()
    try:
        rowcount = conn.execute(
            q(
                "DELETE FROM node_routes WHERE node_id = ? AND path = ? AND method = ?",
                (node_id, path, method),
            ),
        ).rowcount
        conn.commit()
    finally:
        conn.close()

    if rowcount == 0:
        raise HTTPException(status_code=404, detail="route not found")
    logger.info("route deleted: node=%s path=%s method=%s", node_id, path, method)
    return {"status": "deleted", "node_id": node_id, "path": path, "method": method}
