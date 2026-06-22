"""Authentication router."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address

from relay_server.core.auth import (
    _create_token as create_runtime_token,
    generate_secret,
    hash_secret,
)
from relay_server.core.auth import (
    refresh_token,
    register_admin_node,
    register_pending_node,
    validate_token,
)
from relay_server.models import (
    AdminNodeRegistration,
    AdminNodeRegistrationResponse,
    NodeRegistration,
    NodeRegistrationResponse,
    RegistrationStatusRequest,
    RegistrationStatusResponse,
    TokenResponse,
)

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


def _format_time(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _token_response(
    node_id: str,
    node_name: str,
    status: str,
    token_type: str,
    token: str,
    ttl_hours: int,
) -> TokenResponse:
    expires = _now() + timedelta(hours=ttl_hours)
    return TokenResponse(
        node_id=node_id,
        node_name=node_name,
        status=status,
        token_type=token_type,
        token=token,
        expires_at=_format_time(expires),
    )


@router.post("/register", response_model=NodeRegistrationResponse)
@limiter.limit("10/minute")
async def auth_register(request: Request, body: NodeRegistration):
    """Register a new worker/service node.

    The cluster assigns a unique 8-character node_id. The node starts in
    `pending` state and needs admin approval.
    """
    from relay_server.config import settings

    caps = [c.model_dump() for c in body.capabilities]
    node_id, token, registration_secret = register_pending_node(
        node_name=body.node_name,
        endpoint=body.endpoint,
        capabilities=caps,
        role=body.role,
    )
    if not node_id or not token or not registration_secret:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not generate unique node ID",
        )

    temporary_ttl = getattr(settings, "temporary_token_ttl_hours", 24)
    return {
        "node_id": node_id,
        "node_name": body.node_name,
        "status": "pending",
        "token_type": "temporary",
        "token": token,
        "expires_at": _format_time(_now() + timedelta(hours=temporary_ttl)),
        "registration_secret": registration_secret,
    }


@router.post("/register-admin", response_model=AdminNodeRegistrationResponse)
@limiter.limit("5/minute")
async def auth_register_admin(request: Request, body: AdminNodeRegistration):
    """Register an admin node using the master bootstrap secret.

    The cluster assigns a unique 8-character node_id.
    """
    from relay_server.config import settings

    caps = [c.model_dump() for c in body.capabilities]
    node_id, token = register_admin_node(
        node_name=body.node_name,
        bootstrap_secret=body.bootstrap_secret,
        endpoint=body.endpoint,
        capabilities=caps,
    )
    if not node_id or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bootstrap secret or could not generate unique ID",
        )
    return _token_response(
        node_id=node_id,
        node_name=body.node_name,
        status="approved",
        token_type="runtime",
        token=token,
        ttl_hours=settings.token_ttl_hours,
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit("12/minute")
async def auth_refresh(request: Request, authorization: Optional[str] = Header(None)):
    token = _extract_bearer(authorization)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    from relay_server.config import settings

    new_token = refresh_token(token)
    if not new_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    info = validate_token(new_token)
    return _token_response(
        node_id=info["node_id"],
        node_name=info["node_name"],
        status=info["status"],
        token_type="runtime",
        token=new_token,
        ttl_hours=settings.token_ttl_hours,
    )


@router.post("/status", response_model=RegistrationStatusResponse)
@limiter.limit("30/minute")
async def auth_status(request: Request, body: RegistrationStatusRequest):
    """Poll approval status using the long-lived registration secret.

    Returns the runtime token once the node has been approved by an admin.
    """
    from relay_server.config import settings
    from relay_server.core.auth import verify_secret
    from relay_server.core.db import get_conn

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT node_id, node_name, status, role, registration_secret_hash "
            "FROM nodes WHERE node_id = ?",
            (body.node_id,),
        ).fetchone()
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Node not found",
            )
        if not row["registration_secret_hash"]:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Registration secret not available",
            )
        if not verify_secret(body.registration_secret, row["registration_secret_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid registration secret",
            )

        if row["status"] == "pending":
            return RegistrationStatusResponse(
                node_id=row["node_id"],
                node_name=row["node_name"],
                status=row["status"],
                message="Awaiting admin approval",
            )

        # Approved or any other non-pending state: issue a fresh runtime token and
        # rotate the long-lived registration secret so it is single-use.
        token = create_runtime_token(
            node_id=row["node_id"],
            node_name=row["node_name"],
            role=row["role"],
            token_type="runtime",
            pending=False,
            ttl_hours=settings.token_ttl_hours,
        )
        new_registration_secret = generate_secret("rs_")
        conn.execute(
            "UPDATE nodes SET registration_secret_hash = ? WHERE node_id = ?",
            (hash_secret(new_registration_secret), row["node_id"]),
        )
        conn.commit()
        info = validate_token(token, require_approved=False)
        return RegistrationStatusResponse(
            node_id=row["node_id"],
            node_name=row["node_name"],
            status=row["status"],
            token=token,
            token_type="runtime",
            expires_at=info.get("expires_at") if info else "",
            registration_secret=new_registration_secret,
            message="Node approved — runtime token and registration secret rotated",
        )
    finally:
        conn.close()


def _extract_bearer(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip()
