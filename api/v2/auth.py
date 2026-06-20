"""Authentication router."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, status

from relay_server.core.auth import (
    init_master_seed,
    refresh_token,
    register_admin_node,
    register_pending_node,
    validate_token,
)
from relay_server.models import NodeRegistration, TokenResponse

router = APIRouter()


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


@router.post("/init-master")
async def auth_init_master():
    """Initialize the master admin seed. Only callable when no master seed exists."""
    from relay_server.config import settings

    ttl = getattr(settings, "token_ttl_hours", 168)
    secret = init_master_seed()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Master admin seed already initialized",
        )
    return {
        "status": "created",
        "warning": "Store this secret securely. It will not be shown again.",
        "temporary_token_ttl_hours": 24,
        "runtime_token_ttl_hours": ttl,
    }


@router.post("/register", response_model=TokenResponse)
async def auth_register(body: NodeRegistration):
    """Register a new node. Admin nodes need a bootstrap_secret; others start pending."""
    from relay_server.config import settings

    caps = [c.model_dump() for c in body.capabilities]

    if body.bootstrap_secret:
        # Attempt admin registration.
        token = register_admin_node(
            node_id=body.node_id,
            node_name=body.node_name,
            bootstrap_secret=body.bootstrap_secret,
            endpoint=body.endpoint,
            capabilities=caps,
        )
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bootstrap secret or node already exists",
            )
        return _token_response(
            node_id=body.node_id,
            node_name=body.node_name,
            status="approved",
            token_type="runtime",
            token=token,
            ttl_hours=settings.token_ttl_hours,
        )

    # Worker/service registration goes into pending state.
    token = register_pending_node(
        node_id=body.node_id,
        node_name=body.node_name,
        endpoint=body.endpoint,
        capabilities=caps,
        role=body.role,
    )
    if not token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Node already exists",
        )

    temporary_ttl = getattr(settings, "temporary_token_ttl_hours", 24)
    return _token_response(
        node_id=body.node_id,
        node_name=body.node_name,
        status="pending",
        token_type="temporary",
        token=token,
        ttl_hours=temporary_ttl,
    )


@router.post("/refresh", response_model=TokenResponse)
async def auth_refresh(authorization: Optional[str] = Header(None)):
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


def _extract_bearer(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip()
