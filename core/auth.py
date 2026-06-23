"""Authentication and authorization core logic."""

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

from relay_server.config import settings
from relay_server.core.db import get_conn
from relay_server.core.node_registry import NodeRegistry

class NodeExistsError(Exception):
    """Raised when a node_id or node_name already exists during registration."""

    def __init__(self, field: str, value: str):
        self.field = field
        self.value = value
        super().__init__(f"Node with {field}={value} already exists")


ADMIN_SEED_PREFIX = "adm_"
BOOTSTRAP_SEED_PREFIX = "bs_"
TEMPORARY_TOKEN_PREFIX = "tp_"
RUNTIME_TOKEN_PREFIX = "rt_"

# Cluster-side node registry. This is the only object that may mint new node IDs.
_registry = NodeRegistry()


def _mint_node_id() -> str:
    """Generate a unique node ID that does not collide with an existing node."""
    return _registry.generate_unique_node_id(exists_callback=_node_exists)


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
    token = secrets.token_urlsafe(32)
    return f"{prefix}{token}"


def secret_entropy_bits(secret: str) -> int:
    """Return the Shannon-ish entropy of a secret in bits.

    This is a rough estimate based on the printable ASCII range. The actual
    entropy of a generated secret is ``32 * 8 = 256`` bits because the token
    is base64url encoded.
    """
    if not secret:
        return 0
    unique_chars = set(secret)
    if len(unique_chars) <= 1:
        return 0
    import math

    length = len(secret)
    entropy = 0.0
    for ch in unique_chars:
        p = secret.count(ch) / length
        entropy -= p * math.log2(p)
    return int(entropy * length)


def hash_secret(secret: str) -> str:
    """Hash a high-entropy secret for storage using bcrypt (12 rounds).

    This replaces the previous unsalted SHA-256 implementation. bcrypt is a
    deliberately slow hash that raises the cost for an attacker who obtains
    the database. verify_secret() remains compatible with legacy SHA-256 hashes.
    """
    import bcrypt

    return bcrypt.hashpw(secret.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def _hash_with_salt(secret: str) -> str:
    """Deprecated transitional helper.

    Kept only for backward compatibility with databases that already contain
    ``$sha256-salt$`` hashes. New hashes are always bcrypt.
    """
    salt = secrets.token_hex(16)
    digest = hashlib.sha256((salt + secret).encode("utf-8")).hexdigest()
    return f"$sha256-salt${salt}${digest}"


def verify_secret(secret: str, secret_hash: str) -> bool:
    """Verify a secret against a stored hash.

    Accepts legacy unsalted SHA-256, legacy salted SHA-256, and modern bcrypt
    hashes. Legacy formats are verified but new secrets are always stored with
    bcrypt.
    """
    if not secret_hash:
        return False

    # Modern bcrypt hashes.
    if secret_hash.startswith(("$2a$", "$2b$", "$2y$")):
        import bcrypt

        try:
            return bcrypt.checkpw(secret.encode("utf-8"), secret_hash.encode("utf-8"))
        except ValueError:
            return False

    # Legacy salted SHA-256.
    if secret_hash.startswith("$sha256-salt$"):
        _, salt, digest = secret_hash.split("$")
        candidate = hashlib.sha256((salt + secret).encode("utf-8")).hexdigest()
        return secrets.compare_digest(candidate, digest)

    # Legacy unsalted SHA-256.
    return secrets.compare_digest(_sha256(secret), secret_hash)


def _is_legacy_sha256(secret_hash: str) -> bool:
    """Return True if the hash is an unsalted SHA-256 hex digest."""
    return len(secret_hash) == 64 and all(c in "0123456789abcdef" for c in secret_hash.lower())


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
    node_name: str,
    bootstrap_secret: str,
    endpoint: Optional[str],
    capabilities: list,
) -> tuple[Optional[str], Optional[str]]:
    """Register an admin node using the master seed. Returns (node_id, runtime token) or (None, None)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT seed_hash FROM admin_seeds WHERE seed_id = ?", ("master",)
        ).fetchone()
        if not row:
            return None, None
        if not verify_secret(bootstrap_secret, row["seed_hash"]):
            return None, None

        node_id = _mint_node_id()
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
        token = _create_token(
            node_id,
            node_name,
            role="admin",
            token_type="runtime",
            pending=False,
            ttl_hours=settings.token_ttl_hours,
        )
        return node_id, token
    finally:
        conn.close()


def register_pending_node(
    node_name: str,
    endpoint: Optional[str],
    capabilities: list,
    role: str = "worker",
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Register a worker/service node in pending state.

    Returns (node_id, temporary_token, registration_secret) or (None, None, None).
    """
    node_id = _mint_node_id()
    conn = get_conn()
    try:
        now = _now()
        now_str = _format_time(now)
        caps_json = _serialize_capabilities(capabilities)
        registration_secret = generate_secret("rs_")
        rs_expires = now + timedelta(hours=settings.registration_secret_ttl_hours)
        conn.execute(
            """
            INSERT INTO nodes
            (node_id, node_name, endpoint, capabilities, last_seen, registered_at, status, role,
             registration_secret_hash, registration_secret_expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node_id,
                node_name,
                endpoint,
                caps_json,
                now_str,
                now_str,
                "pending",
                role,
                hash_secret(registration_secret),
                _format_time(rs_expires),
            ),
        )
        conn.commit()

        temporary_ttl = getattr(settings, "temporary_token_ttl_hours", 24)
        token = _create_token(
            node_id,
            node_name,
            role=role,
            token_type="temporary",
            pending=True,
            ttl_hours=temporary_ttl,
        )
        return node_id, token, registration_secret
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        detail = str(exc)
        if "node_id" in detail:
            raise NodeExistsError("node_id", node_id)
        if "node_name" in detail:
            raise NodeExistsError("node_name", node_name)
        raise NodeExistsError("node", node_id)
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
    conn = get_conn()
    try:
        # Query candidate tokens and compare bcrypt hashes in constant time.
        # Deterministic hash lookups do not exist for bcrypt because of random salts.
        token_rows = conn.execute(
            """
            SELECT token_id, node_id, node_name, token_type, pending, role, expires_at, token_hash
            FROM node_tokens
            WHERE expires_at > ? OR expires_at IS NULL
            """,
            (_format_time(_now() - timedelta(seconds=1)),),
        ).fetchall()

        token_row = None
        for row in token_rows:
            if verify_secret(token, row["token_hash"]):
                token_row = row
                break

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
            if result["pending"] or result["status"] not in ("approved", "online"):
                return None

        return result
    finally:
        conn.close()


def refresh_token(token: str) -> Optional[str]:
    """Refresh a runtime token. Returns new token or None.

    Invalidates all existing runtime tokens for the node so that only one
    runtime token is valid at any time.
    """
    info = validate_token(token, require_approved=True)
    if not info:
        return None

    return _replace_runtime_token(info["node_id"], info["node_name"], info["role"])


def _replace_runtime_token(node_id: str, node_name: str, role: str) -> str:
    """Delete all runtime tokens for a node and create a single new one."""
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM node_tokens WHERE node_id = ? AND token_type = ?",
            (node_id, "runtime"),
        )
        conn.commit()
        return _create_token(
            node_id,
            node_name,
            role=role,
            token_type="runtime",
            pending=False,
            ttl_hours=settings.token_ttl_hours,
        )
    finally:
        conn.close()


def rotate_registration_secret(node_id: str) -> Optional[str]:
    """Generate a new registration secret for an approved node. Returns the plain secret."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT status FROM nodes WHERE node_id = ?", (node_id,)
        ).fetchone()
        if not row or row["status"] != "approved":
            return None

        new_secret = generate_secret("rs_")
        expires = _now() + timedelta(hours=settings.registration_secret_ttl_hours)
        conn.execute(
            "UPDATE nodes SET registration_secret_hash = ?, registration_secret_expires_at = ? WHERE node_id = ?",
            (hash_secret(new_secret), _format_time(expires), node_id),
        )
        conn.commit()
        return new_secret
    finally:
        conn.close()


def get_registration_secret_expiry(node_id: str) -> Optional[datetime]:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT registration_secret_expires_at FROM nodes WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if not row or not row["registration_secret_expires_at"]:
            return None
        return _parse_time(row["registration_secret_expires_at"])
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


def login_with_master_seed(seed: str) -> Optional[str]:
    """Validate the master admin seed and create a runtime admin token.

    This is intended for dashboard/browser login. It does not create a new
    node entry; instead it mints a short-lived runtime token bound to a
    synthetic admin node id.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT seed_hash, role FROM admin_seeds WHERE seed_id = ?", ("master",)
        ).fetchone()
        if not row:
            return None
        if not verify_secret(seed, row["seed_hash"]):
            return None

        # Use a deterministic synthetic admin node for dashboard sessions.
        dashboard_node_id = "__dashboard_admin__"
        node_row = conn.execute(
            "SELECT node_id, node_name FROM nodes WHERE node_id = ?", (dashboard_node_id,)
        ).fetchone()
        if not node_row:
            now = _format_time(_now())
            conn.execute(
                """
                INSERT INTO nodes
                (node_id, node_name, endpoint, capabilities, last_seen, registered_at, status, role)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (dashboard_node_id, "Dashboard Admin", None, "[]", now, now, "approved", "admin"),
            )
            conn.commit()
            node_name = "Dashboard Admin"
        else:
            node_name = node_row["node_name"]

        return _create_token(
            dashboard_node_id,
            node_name,
            role="admin",
            token_type="runtime",
            pending=False,
            ttl_hours=settings.token_ttl_hours,
        )
    finally:
        conn.close()
