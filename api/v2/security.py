"""Security dependencies for FastAPI endpoints."""

from typing import Optional

from fastapi import Cookie, Header, HTTPException, status

from relay_server.core.auth import validate_token
from relay_server.models import AuthContext


def extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip()


async def get_auth_context(
    authorization: Optional[str] = Header(None),
    relay_token: Optional[str] = Cookie(None),
) -> AuthContext:
    """Dependency: authenticate any valid token (pending or approved)."""
    token = extract_bearer(authorization) or relay_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    info = validate_token(token, require_approved=False)
    if not info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    return AuthContext(**info)


async def get_approved_context(
    authorization: Optional[str] = Header(None),
    relay_token: Optional[str] = Cookie(None),
) -> AuthContext:
    """Dependency: authenticate and require approved node status."""
    token = extract_bearer(authorization) or relay_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )

    # First check if token is valid at all.
    info = validate_token(token, require_approved=False)
    if not info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

    # Then check approval status.
    if info["pending"] or info["status"] not in ("approved", "online"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Node not approved",
        )

    return AuthContext(**info)


async def require_admin(
    authorization: Optional[str] = Header(None),
    relay_token: Optional[str] = Cookie(None),
) -> AuthContext:
    """Dependency: require admin role and approved status."""
    ctx = await get_approved_context(authorization, relay_token)
    if not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return ctx
