"""Human user management and RBAC for the relay dashboard."""

import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import bcrypt

from relay_server.core.auth import login_with_master_seed
from relay_server.core.db import get_conn
from relay_server.models import AuthContext


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8)}"


def _validate_username(username: str) -> bool:
    return bool(re.match(r"^[a-zA-Z0-9_.-]{3,40}$", username))


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def create_user(
    username: str,
    password: str,
    group_names: Optional[List[str]] = None,
    email: Optional[str] = None,
    created_by: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new human user. Returns user info."""
    if not _validate_username(username):
        raise ValueError("Invalid username")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")

    user_id = _generate_id("usr")
    now = _now()
    password_hash = _hash_password(password)

    group_names = group_names or ["user"]

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO users (user_id, username, email, password_hash, is_active, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, email, password_hash, 1, now, created_by),
        )

        for group_name in group_names:
            group_row = conn.execute(
                "SELECT group_id FROM groups WHERE group_name = ?", (group_name,)
            ).fetchone()
            if not group_row:
                raise ValueError(f"Unknown group: {group_name}")
            conn.execute(
                "INSERT INTO user_groups (user_id, group_id, granted_at) VALUES (?, ?, ?)",
                (user_id, group_row["group_id"], now),
            )

        conn.commit()
    except sqlite3.IntegrityError as e:
        raise ValueError("Username already exists") from e
    finally:
        conn.close()

    return {"user_id": user_id, "username": username, "email": email, "groups": group_names}


def authenticate_user(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Authenticate a human user by username/password."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT user_id, username, email, password_hash, is_active FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if not row or not row["is_active"]:
            return None
        if not _verify_password(password, row["password_hash"]):
            return None
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "email": row["email"],
            "is_active": bool(row["is_active"]),
        }
    finally:
        conn.close()


def authenticate_master_seed(seed: str) -> Optional[Dict[str, Any]]:
    """Authenticate via master seed. Returns a synthetic admin user info."""
    token = login_with_master_seed(seed)
    if not token:
        return None
    return {
        "user_id": "__master__",
        "username": "master",
        "email": None,
        "is_active": True,
        "is_master": True,
    }


def get_user_permissions(user_id: str) -> List[str]:
    """Return permission names for a user via groups."""
    if user_id == "__master__":
        return list(_all_permission_names())

    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT p.permission_name
            FROM permissions p
            JOIN group_permissions gp ON gp.permission_id = p.permission_id
            JOIN user_groups ug ON ug.group_id = gp.group_id
            WHERE ug.user_id = ?
            """,
            (user_id,),
        ).fetchall()
        return [r["permission_name"] for r in rows]
    finally:
        conn.close()


def has_permission(user_id: str, permission: str) -> bool:
    """Check if a user has a specific permission."""
    if user_id == "__master__":
        return True
    return permission in get_user_permissions(user_id)


def has_any_permission(user_id: str, permissions: List[str]) -> bool:
    """Check if a user has any of the listed permissions."""
    if user_id == "__master__":
        return True
    user_perms = set(get_user_permissions(user_id))
    return bool(user_perms & set(permissions))


def list_users() -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT u.user_id, u.username, u.email, u.is_active, u.created_at, u.created_by,
                   GROUP_CONCAT(g.group_name, ',') as groups
            FROM users u
            LEFT JOIN user_groups ug ON ug.user_id = u.user_id
            LEFT JOIN groups g ON g.group_id = ug.group_id
            GROUP BY u.user_id
            ORDER BY u.created_at DESC
            """
        ).fetchall()
        return [
            {
                "user_id": r["user_id"],
                "username": r["username"],
                "email": r["email"],
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"],
                "created_by": r["created_by"],
                "groups": (r["groups"] or "").split(",") if r["groups"] else [],
            }
            for r in rows
        ]
    finally:
        conn.close()


def list_groups() -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT g.group_id, g.group_name, g.description, g.created_at,
                   GROUP_CONCAT(p.permission_name, ',') as permissions
            FROM groups g
            LEFT JOIN group_permissions gp ON gp.group_id = g.group_id
            LEFT JOIN permissions p ON p.permission_id = gp.permission_id
            GROUP BY g.group_id
            ORDER BY g.created_at DESC
            """
        ).fetchall()
        return [
            {
                "group_id": r["group_id"],
                "group_name": r["group_name"],
                "description": r["description"],
                "created_at": r["created_at"],
                "permissions": (r["permissions"] or "").split(",") if r["permissions"] else [],
            }
            for r in rows
        ]
    finally:
        conn.close()


def list_permissions() -> List[Dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT permission_id, permission_name, description FROM permissions ORDER BY permission_name"
        ).fetchall()
        return [
            {
                "permission_id": r["permission_id"],
                "permission_name": r["permission_name"],
                "description": r["description"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def _all_permission_names() -> List[str]:
    return [p["permission_name"] for p in list_permissions()]


def set_user_groups(user_id: str, group_names: List[str]) -> None:
    conn = get_conn()
    try:
        group_ids = []
        for name in group_names:
            row = conn.execute(
                "SELECT group_id FROM groups WHERE group_name = ?", (name,)
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown group: {name}")
            group_ids.append(row["group_id"])

        conn.execute("DELETE FROM user_groups WHERE user_id = ?", (user_id,))
        now = _now()
        for group_id in group_ids:
            conn.execute(
                "INSERT INTO user_groups (user_id, group_id, granted_at) VALUES (?, ?, ?)",
                (user_id, group_id, now),
            )
        conn.commit()
    finally:
        conn.close()


def set_group_permissions(group_id: str, permission_names: List[str]) -> None:
    conn = get_conn()
    try:
        perm_ids = []
        for name in permission_names:
            row = conn.execute(
                "SELECT permission_id FROM permissions WHERE permission_name = ?", (name,)
            ).fetchone()
            if not row:
                raise ValueError(f"Unknown permission: {name}")
            perm_ids.append(row["permission_id"])

        conn.execute("DELETE FROM group_permissions WHERE group_id = ?", (group_id,))
        now = _now()
        for perm_id in perm_ids:
            conn.execute(
                "INSERT INTO group_permissions (group_id, permission_id, granted_at) VALUES (?, ?, ?)",
                (group_id, perm_id, now),
            )
        conn.commit()
    finally:
        conn.close()


def set_user_password(user_id: str, password: str) -> None:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")
    conn = get_conn()
    try:
        password_hash = _hash_password(password)
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE user_id = ?",
            (password_hash, user_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_active(user_id: str, is_active: bool) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE users SET is_active = ? WHERE user_id = ?", (1 if is_active else 0, user_id)
        )
        conn.commit()
    finally:
        conn.close()


def delete_user(user_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def require_permission(ctx: AuthContext, permission: str):
    if not has_permission(ctx.user_id, permission):
        raise PermissionError(f"Missing permission: {permission}")
