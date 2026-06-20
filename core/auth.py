"""Authentication and authorization core logic."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from relay_server.config import settings
from relay_server.core.db import get_conn

ADMIN_SEED_PREFIX = "adm_"
BOOTSTRAP_SEED_PREFIX = "bs_"
TEMPORARY_TOKEN_PREFIX = "tp_"
RUNTIME_TOKEN_PREFIX = "rt_"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(dt: datetime) -> str:
    return dt.isoformat()


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def generate_secret(prefix: str = "sec") -> str:
    """Generate a cryptographically secure secret string."""
    return f"{prefix}{secrets.token_urlsafe(32)}"


def hash_secret(secret: str) -> str:
    """Hash a secret for storage."""
    return _sha256(secret)


def verify_secret(secret: str, secret_hash: str) -> bool:
    """Verify a secret against a stored hash."""
    return secrets.compare_digest(hash_secret(secret), secret_hash)


def _token_id() -> str:
    return secrets.token_urlsafe(16)


def init_master_seed() -> Optional[str]:
    """Create the master admin seed if none exists. Returns the secret once."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT seed_id FROM admin_seeds WHERE seed_id = ?", ("master",)
        ).fetchone()
        if row:
            return None

        secret = generate_secret(ADMIN_SEED_PREFIX)
        secret_hash = hash_secret(secret)
        now = _format_time(_now())
        conn.execute(
            "INSERT INTO admin_seeds (seed_id, seed_hash, role, created_at) VALUES (?, ?, ?, ?)",
            ("master", secret_hash, "admin", now),
        )
        conn.commit()
        return secret
    finally:
        conn.close()


def _create_token(
    node_id: str,
    node_name: str,
    role: str,
    token_type: str,
    pending: bool,
    ttl_hours: int,
) -> str:
    """Create a new token, store its hash, return the plain token."""
    conn = get_conn()
    try:
        token_id = _token_id()
        prefix = TEMPORARY_TOKEN_PREFIX if token_type == "temporary" else RUNTIME_TOKEN_PREFIX
        token = f"{prefix}{secrets.token_urlsafe(32)}"
        token_hash = hash_secret(token)
        now = _now()
        expires = now + timedelta(hours=ttl_hours)

        conn.execute(
            """
            INSERT INTO node_tokens
            (token_id, node_id, node_name, token_hash, token_type, pending, role, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token_id,
                node_id,
                node_name,
                token_hash,
                token_type,
                1 if pending else 0,
                role,
                _format_time(expires),
                _format_time(now),
            ),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def _node_exists(node_id: str) -> bool:
    conn = get_conn()
    try:
        row = conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def register_admin_node(
    node_id: str,
    node_name: str,
    bootstrap_secret: str,
    endpoint: Optional[str],
    capabilities: list,
) -> Optional[str]:
    """Register an admin node using the master seed. Returns runtime token or None."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT seed_hash FROM admin_seeds WHERE seed_id = ?", ("master",)
        ).fetchone()
        if not row:
            return None
        if not verify_secret(bootstrap_secret, row["seed_hash"]):
            return None
        if _node_exists(node_id):
            return None

        now = _format_time(_now())
        caps_json = _serialize_capabilities(capabilities)
        conn.execute(
            """
            INSERT INTO nodes
            (node_id, node_name, endpoint, capabilities, last_seen, registered_at, status, role)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (node_id, node_name, endpoint, caps_json, now, now, "approved", "admin"),
        )
        conn.commit()
        return _create_token(
            node_id,
            node_name,
            role="admin",
            token_type="runtime",
            pending=False,
            ttl_hours=settings.token_ttl_hours,
        )
    finally:
        conn.close()


def register_pending_node(
    node_id: str,
    node_name: str,
    endpoint: Optional[str],
    capabilities: list,
    role: str = "worker",
) -> Optional[str]:
    """Register a worker/service node in pending state. Returns temporary token."""
    if _node_exists(node_id):
        return None

    conn = get_conn()
    try:
        now = _format_time(_now())
        caps_json = _serialize_capabilities(capabilities)
        conn.execute(
            """
            INSERT INTO nodes
            (node_id, node_name, endpoint, capabilities, last_seen, registered_at, status, role)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (node_id, node_name, endpoint, caps_json, now, now, "pending", role),
        )
        conn.commit()

        temporary_ttl = getattr(settings, "temporary_token_ttl_hours", 24)
        return _create_token(
            node_id,
            node_name,
            role=role,
            token_type="temporary",
            pending=True,
            ttl_hours=temporary_ttl,
        )
    finally:
        conn.close()


def approve_node(
    node_id: str,
    role: Optional[str] = None,
    capabilities: Optional[list] = None,
    endpoint: Optional[str] = None,
) -> Optional[str]:
    """Approve a pending node and return a runtime token."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT node_name, role AS current_role, capabilities AS current_caps, endpoint AS current_endpoint, status "
            "FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if not row:
            return None
        if row["status"] != "pending":
            return None

        final_role = role or row["current_role"]
        final_caps = _serialize_capabilities(capabilities) if capabilities else row["current_caps"]
        final_endpoint = endpoint if endpoint is not None else row["current_endpoint"]

        now = _format_time(_now())
        conn.execute(
            """
            UPDATE nodes
            SET status = ?, role = ?, capabilities = ?, endpoint = ?, last_seen = ?
            WHERE node_id = ?
            """,
            ("approved", final_role, final_caps, final_endpoint, now, node_id),
        )
        # Invalidate any existing temporary tokens for this node.
        conn.execute(
            "DELETE FROM node_tokens WHERE node_id = ? AND token_type = ?",
            (node_id, "temporary"),
        )
        conn.commit()

        return _create_token(
            node_id,
            row["node_name"],
            role=final_role,
            token_type="runtime",
            pending=False,
            ttl_hours=settings.token_ttl_hours,
        )
    finally:
        conn.close()


def validate_token(token: str, require_approved: bool = True) -> Optional[dict]:
    """Validate a bearer token. Returns node info or None."""
    token_hash = hash_secret(token)
    conn = get_conn()
    try:
        token_row = conn.execute(
            """
            SELECT token_id, node_id, node_name, token_type, pending, role, expires_at
            FROM node_tokens
            WHERE token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if not token_row:
            return None

        expires = _parse_time(token_row["expires_at"])
        if expires and _now() > expires:
            conn.execute("DELETE FROM node_tokens WHERE token_id = ?", (token_row["token_id"],))
            conn.commit()
            return None

        node_row = conn.execute(
            "SELECT node_id, node_name, endpoint, capabilities, status, role FROM nodes WHERE node_id = ?",
            (token_row["node_id"],),
        ).fetchone()
        if not node_row:
            return None

        result = {
            "token_id": token_row["token_id"],
            "node_id": node_row["node_id"],
            "node_name": node_row["node_name"],
            "endpoint": node_row["endpoint"],
            "capabilities": _parse_capabilities(node_row["capabilities"]),
            "status": node_row["status"],
            "role": node_row["role"],
            "token_type": token_row["token_type"],
            "pending": bool(token_row["pending"]),
            "expires_at": token_row["expires_at"],
        }

        if require_approved:
            if result["pending"] or result["status"] != "approved":
                return None

        return result
    finally:
        conn.close()


def refresh_token(token: str) -> Optional[str]:
    """Refresh a runtime token. Returns new token or None."""
    info = validate_token(token, require_approved=True)
    if not info:
        return None

    conn = get_conn()
    try:
        # Invalidate old token.
        old_hash = hash_secret(token)
        conn.execute("DELETE FROM node_tokens WHERE token_hash = ?", (old_hash,))
        conn.commit()
        return _create_token(
            info["node_id"],
            info["node_name"],
            role=info["role"],
            token_type="runtime",
            pending=False,
            ttl_hours=settings.token_ttl_hours,
        )
    finally:
        conn.close()


def _serialize_capabilities(capabilities: list) -> str:
    import json

    return json.dumps(capabilities)


def _parse_capabilities(value: Optional[str]) -> list:
    import json

    if not value:
        return []
    try:
        return json.loads(value)
    except Exception:
        return []


def is_admin(token_info: dict) -> bool:
    return token_info.get("role") == "admin" and token_info.get("status") == "approved"
