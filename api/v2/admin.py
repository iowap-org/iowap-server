"""Administration router for node approval and cluster management."""

from fastapi import APIRouter, Depends, HTTPException, status

from relay_server.api.v2.security import get_auth_context, require_admin
from relay_server.core.auth import approve_node
from relay_server.models import AuthContext, NodeApproval, TokenResponse

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/nodes")
async def admin_list_nodes(ctx: AuthContext = Depends(get_auth_context)):
    from relay_server.core.db import get_conn

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT node_id, node_name, endpoint, capabilities, status, role, last_seen "
            "FROM nodes ORDER BY registered_at DESC"
        ).fetchall()
        return {
            "nodes": [
                {
                    "node_id": r["node_id"],
                    "node_name": r["node_name"],
                    "endpoint": r["endpoint"],
                    "capabilities": _parse_caps(r["capabilities"]),
                    "status": r["status"],
                    "role": r["role"],
                    "last_seen": r["last_seen"],
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


@router.post("/nodes/{node_id}/approve", response_model=TokenResponse)
async def admin_approve_node(
    node_id: str,
    body: NodeApproval,
    ctx: AuthContext = Depends(get_auth_context),
):

    caps = [c.model_dump() for c in body.capabilities] if body.capabilities else None
    token = approve_node(
        node_id=node_id,
        role=body.role,
        capabilities=caps,
        endpoint=body.endpoint,
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pending node not found or already approved",
        )
    return _build_token_response(token)


@router.post("/nodes/{node_id}/token", response_model=TokenResponse)
async def admin_issue_node_token(
    node_id: str,
    ctx: AuthContext = Depends(get_auth_context),
):
    """Issue a new runtime token for an already approved node."""
    from relay_server.core.auth import _create_token
    from relay_server.core.db import get_conn
    from relay_server.config import settings

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT node_id, node_name, role FROM nodes WHERE node_id = ? AND status IN (?, ?)",
            (node_id, "approved", "offline"),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Approved/offline node not found",
            )
        # Invalidate old runtime tokens for this node to avoid ambiguity.
        conn.execute(
            "DELETE FROM node_tokens WHERE node_id = ? AND token_type = ?",
            (node_id, "runtime"),
        )
        conn.commit()
        token = _create_token(
            node_id=row["node_id"],
            node_name=row["node_name"],
            role=row["role"],
            token_type="runtime",
            pending=False,
            ttl_hours=settings.token_ttl_hours,
        )
    finally:
        conn.close()

    return _build_token_response(token)


def _build_token_response(token: str) -> TokenResponse:
    info = validate_token_safe(token)
    return TokenResponse(
        node_id=info["node_id"],
        node_name=info["node_name"],
        status=info["status"],
        token_type="runtime",
        token=token,
        expires_at=info.get("expires_at", ""),
    )


def validate_token_safe(token: str):
    from relay_server.core.auth import validate_token

    info = validate_token(token, require_approved=False)
    if not info:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Token created but could not be validated",
        )
    return info


def _parse_caps(value):
    import json

    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        return []
