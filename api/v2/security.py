"""Security dependencies for FastAPI endpoints."""

from typing import Optional

from fastapi import Cookie, Header, HTTPException, status

from relay_server.core.auth import validate_token
from relay_server.core.session import unsign_user_cookie
from relay_server.core.users import get_user_permissions
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
            detail="Missing or invalid authentication",
        )

    info = validate_token(token, require_approved=False)
    if not info:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )

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
    """Dependency: require admin role and approved status (node admin)."""
    ctx = await get_approved_context(authorization, relay_token)
    if not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return ctx


async def require_dashboard_user(
    authorization: Optional[str] = Header(None),
    relay_token: Optional[str] = Cookie(None),
    relay_user: Optional[str] = Cookie(None),
) -> AuthContext:
    """Dependency: authenticate dashboard user via human user cookie or node token.

    Human session cookies take precedence over node runtime tokens so that a
    stale admin node token cannot override a currently logged-in human user.
    """
    from relay_server.core.users import list_users

    # Human user path takes precedence over node tokens.
    if relay_user:
        user_data = unsign_user_cookie(relay_user)
        if user_data is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session cookie",
            )
        user_id = user_data.get("user_id")
        username = user_data.get("username")

        if user_id == "__master__" and username == "master":
            return AuthContext(
                token_id=user_id,
                node_id=user_id,
                node_name=username,
                endpoint=None,
                capabilities=[],
                status="approved",
                role="admin",
                token_type="user_session",
                pending=False,
                user_id=user_id,
                username=username,
            )

        # Verify user still exists and is active.
        for user in list_users():
            if user["user_id"] == user_id and user["username"] == username and user["is_active"]:
                return AuthContext(
                    token_id=user_id,
                    node_id=user_id,
                    node_name=username or user_id,
                    endpoint=None,
                    capabilities=[],
                    status="approved",
                    role="admin" if "admin" in user.get("groups", []) else "user",
                    token_type="user_session",
                    pending=False,
                    user_id=user_id,
                    username=username,
                )

    # Node token fallback (including admin runtime tokens).
    token = extract_bearer(authorization) or relay_token
    if token:
        info = validate_token(token, require_approved=False)
        if info:
            if info["pending"] or info["status"] not in ("approved", "online"):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Node not approved",
                )
            if info["role"] != "admin":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Dashboard requires admin node",
                )
            return AuthContext(**info)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid authentication",
    )


def check_dashboard_permission(ctx: AuthContext, permission: str) -> None:
    """Check a dashboard permission; raises 403 if missing."""
    if ctx.user_id == "__master__":
        return
    if ctx.user_id:
        permissions = get_user_permissions(ctx.user_id)
        if permission in permissions:
            return
    # Fallback: admin node token.
    if ctx.role == "admin" and ctx.status in ("approved", "online"):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {permission}",
    )
