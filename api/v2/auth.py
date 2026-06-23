"""Authentication router."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from slowapi import Limiter
from slowapi.util import get_remote_address

from relay_server.core.auth import (
    NodeExistsError,
    _create_token as create_runtime_token,
    _replace_runtime_token,
    generate_secret,
    hash_secret,
    register_admin_node,
    register_pending_node,
    rotate_registration_secret,
    validate_token,
)
from relay_server.models import (
    AdminNodeRegistration,
    AdminNodeRegistrationResponse,
    NodeRegistration,
    NodeRegistrationResponse,
    RefreshRequest,
    RefreshResponse,
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
    try:
        node_id, token, registration_secret = register_pending_node(
            node_name=body.node_name,
            endpoint=body.endpoint,
            capabilities=caps,
            role=body.role,
        )
    except NodeExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{exc.field} already exists: {exc.value}",
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


@router.post("/refresh", response_model=RefreshResponse)
@limiter.limit("30/minute")
async def auth_refresh(
    request: Request,
    body: RefreshRequest,
    authorization: Optional[str] = Header(None),
):
    """Refresh a node credential.

    The worker always prefers the runtime token. Two flows are supported:

    1. Runtime-token refresh (normal case):
       Authorization: Bearer <rt_...>
       body.requested_credential = "runtime_token"
       -> returns a new runtime token; the old one is invalidated immediately.

    2. Registration-secret recovery (rt lost but rs still valid):
       Authorization: omitted or invalid
       body.registration_secret = "rs_..."
       body.requested_credential = "runtime_token"
       -> returns a new runtime token and rotates the registration secret.

    3. Registration-secret rotation (proactive, using valid rt):
       Authorization: Bearer <rt_...>
       body.requested_credential = "registration_secret"
       -> returns a new registration secret.
    """
    from relay_server.config import settings
    from relay_server.core.auth import verify_secret
    from relay_server.core.db import get_conn

    requested = body.requested_credential
    bearer = _extract_bearer(authorization)

    conn = get_conn()
    try:
        # Case 1 + 3: authenticated via runtime token.
        if bearer:
            info = validate_token(bearer, require_approved=True)
            if not info:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired runtime token",
                )
            node_id = info["node_id"]
            node_name = info["node_name"]
            role = info["role"]

            if requested == "runtime_token":
                new_token = _replace_runtime_token(node_id, node_name, role)
                info = validate_token(new_token, require_approved=False)
                return RefreshResponse(
                    node_id=node_id,
                    node_name=node_name,
                    token_type="runtime",
                    token=new_token,
                    expires_at=info.get("expires_at") if info else None,
                    message="Runtime token rotated",
                )

            if requested == "registration_secret":
                new_secret = rotate_registration_secret(node_id)
                if not new_secret:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Node not approved",
                    )
                return RefreshResponse(
                    node_id=node_id,
                    node_name=node_name,
                    token_type="registration_secret",
                    token=new_secret,
                    expires_at=_format_time(_now() + timedelta(hours=settings.registration_secret_ttl_hours)),
                    message="Registration secret rotated",
                )

        # Case 2: recovery via registration secret.
        if requested == "runtime_token" and body.registration_secret:
            if not body.node_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="node_id required when authenticating with registration_secret",
                )
            row = conn.execute(
                "SELECT node_id, node_name, status, role, registration_secret_hash, registration_secret_expires_at "
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
            rs_expires = None
            if row["registration_secret_expires_at"]:
                try:
                    rs_expires = datetime.fromisoformat(row["registration_secret_expires_at"])
                except Exception:
                    rs_expires = None
            if rs_expires and _now() > rs_expires:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Registration secret expired",
                )
            if not verify_secret(body.registration_secret, row["registration_secret_hash"]):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid registration secret",
                )
            if row["status"] != "approved":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Node not approved",
                )

            new_token = _replace_runtime_token(row["node_id"], row["node_name"], row["role"])
            new_secret = rotate_registration_secret(row["node_id"])
            info = validate_token(new_token, require_approved=False)
            return RefreshResponse(
                node_id=row["node_id"],
                node_name=row["node_name"],
                token_type="runtime",
                token=new_token,
                expires_at=info.get("expires_at") if info else None,
                message="Runtime token recovered; registration secret rotated",
            )

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid credentials",
        )
    finally:
        conn.close()


@router.post("/status", response_model=RegistrationStatusResponse)
@limiter.limit("30/minute")
async def auth_status(
    request: Request,
    body: RegistrationStatusRequest,
    authorization: Optional[str] = Header(None),
):
    """Poll credential lifetimes for a node.

    Must be called with a valid runtime token in the Authorization header.
    The endpoint is read-only: it never rotates or invalidates credentials.
    Workers should call this every ~2 hours to decide when to refresh the
    runtime token or the registration secret before they expire.
    """
    bearer = _extract_bearer(authorization)
    if not bearer:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    info = validate_token(bearer, require_approved=True)
    if not info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired runtime token",
        )

    node_id = info["node_id"]
    node_name = info["node_name"]

    # Find the currently valid runtime token expiry for this node.
    rt_expires = info.get("expires_at")
    rs_expires_str = None

    from relay_server.core.db import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT registration_secret_expires_at FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row:
            rs_expires_str = row["registration_secret_expires_at"]
    finally:
        conn.close()

    return RegistrationStatusResponse(
        node_id=node_id,
        node_name=node_name,
        status=info["status"],
        rt_valid_until=rt_expires,
        rs_valid_until=rs_expires_str,
        message="Credential status",
    )


def _extract_bearer(header: Optional[str]) -> Optional[str]:
    if not header:
        return None
    if not header.lower().startswith("bearer "):
        return None
    return header[7:].strip()
