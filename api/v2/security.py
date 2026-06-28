"""Security dependencies for FastAPI endpoints."""

from typing import Optional

from fastapi import Cookie, Header, HTTPException, status

from relay_server.core.auth import validate_token
from relay_server.core.session import unsign_user_cookie
from relay_server.core.users import get_user_permissions, list_users
from relay_server.models import AuthContext


def extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    return authorization[7:].strip()


async def get_auth_context(
    authorization: Optional[str] = Header(None),
) -> AuthContext:
    """Dependency: authenticate any valid token (pending or approved)."""
    token = extract_bearer(authorization)
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
) -> AuthContext:
    """Dependency: authenticate and require approved node status."""
    token = extract_bearer(authorization)
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
        status_hint = (
            "awaiting admin approval" if info["pending"] else f"status is {info['status']}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Node not approved ({status_hint})",
        )

    return AuthContext(**info)


async def require_admin(
    authorization: Optional[str] = Header(None),
) -> AuthContext:
    """Dependency: require admin role and approved status (node admin)."""
    ctx = await get_approved_context(authorization)
    if not ctx.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return ctx


async def require_dashboard_user(
    authorization: Optional[str] = Header(None),
    relay_user: Optional[str] = Cookie(None),
) -> AuthContext:
    """Dependency: authenticate a human dashboard user via signed cookie only.

    Node runtime tokens are intentionally NOT accepted for the dashboard; they
    are used via the Authorization header for API calls. This keeps dashboard
    sessions separate from node tokens and avoids storing a node token in a
    browser cookie.
    """
    if not relay_user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid dashboard session",
        )

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
                groups=user.get("groups", []),
                force_password_change=user.get("force_password_change", False),
            )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired dashboard session",
    )


async def require_admin_or_dashboard_user(
    authorization: Optional[str] = Header(None),
    relay_user: Optional[str] = Cookie(None),
) -> AuthContext:
    """Dependency: authenticate an admin action via human cookie or node token.

    Admin API endpoints may be called either by a logged-in human dashboard user
    or by an approved admin node presenting a valid bearer token. Service nodes
    without the admin role are rejected.
    """
    # Prefer human session cookie when present.
    if relay_user:
        try:
            return await require_dashboard_user(authorization, relay_user)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_401_UNAUTHORIZED:
                raise

    # Fall back to node bearer token; must be an approved admin node.
    token = extract_bearer(authorization)
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
                    detail="Admin role required",
                )
            return AuthContext(**info)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid authentication",
    )


# Erlaubte Permissions fuer Admin-Node-Tokens (ohne menschlichen User)
ADMIN_NODE_PERMISSIONS: set[str] = {
    "nodes:approve",
    "nodes:token",
    "nodes:delete",
    "dashboard:view",
}

def check_dashboard_permission(ctx: AuthContext, permission: str) -> None:
    """Check a dashboard permission; raises 403 if missing."""
    if ctx.user_id == "__master__":
        return
    if ctx.user_id:
        permissions = get_user_permissions(ctx.user_id)
        if permission in permissions:
            return
    # Eingeschraenkter Fallback: admin node token darf nur Node-Management.
    if ctx.role == "admin" and ctx.status in ("approved", "online"):
        if permission in ADMIN_NODE_PERMISSIONS:
            return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {permission}",
    )
