"""Database layer — SQLite connection pool and core schema management."""

import functools
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional

from relay_server.config import settings


# ---------------------------------------------------------------------------
# Secret redaction for audit logs (T-024)
# ---------------------------------------------------------------------------
#
# Audit log ``details`` are free-form strings written by admin endpoints.
# In principle no endpoint should ever put a raw secret there, but as
# defense-in-depth we scan the string for known secret patterns and
# replace them with ``[REDACTED]`` before persisting it. This prevents
# tokens, registration secrets or master seeds from leaking into the
# audit table if a future caller is careless.

_SECRET_PATTERNS = [
    # Runtime / temporary / admin tokens issued by auth.py.
    # Prefixes: rt_, tp_, adm_, bs_ followed by >= 16 urlsafe chars.
    re.compile(r"\b(?:rt|tp|adm|bs)_[A-Za-z0-9_\-]{16,}\b"),
    # Registration secrets: rs_<base64url(32)>.
    re.compile(r"\brs_[A-Za-z0-9_\-]{16,}\b"),
    # Generated secrets from generate_secret(): sec_<base64url(32)>.
    re.compile(r"\bsec_[A-Za-z0-9_\-]{16,}\b"),
    # Bearer Authorization header values.
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9_\-\.=]+"),
    # password=... / secret=... / seed=... key=value pairs.
    re.compile(r"(?i)(password|secret|seed|token)\s*[:=]\s*\S+"),
]

_REDACTED = "[REDACTED]"


def _redact_secrets(value: Optional[str]) -> Optional[str]:
    """Return a copy of ``value`` with known secret patterns redacted.

    Used by :func:`log_audit_event` before the ``details`` field is
    written to the database. Returns ``None`` unchanged.
    """
    if not value:
        return value
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    return redacted


# ---------------------------------------------------------------------------
# Retry helper for SQLite lock contention
# ---------------------------------------------------------------------------

LOCKED_RETRIES = 5
LOCKED_BASE_DELAY = 0.05  # 50ms initial, ~1.5s total with backoff


def retry_on_locked(func):
    """Decorator: retry a DB write function on ``database is locked``.

    Uses exponential backoff (50ms -> 100ms -> 200ms -> 400ms -> 800ms).
    Raises the original ``sqlite3.OperationalError`` if all retries are
    exhausted.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_error = None
        delay = LOCKED_BASE_DELAY
        for attempt in range(LOCKED_RETRIES):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                msg = str(exc)
                if "database is locked" not in msg and "locked" not in msg:
                    raise
                last_error = exc
                if attempt < LOCKED_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
        raise last_error  # type: ignore[misc]

    return wrapper


def get_conn() -> sqlite3.Connection:
    """Create a fresh database connection with WAL mode and Row factory."""
    db_path = settings.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Initialize core tables for the relay server."""
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        _schema(conn)
    finally:
        conn.close()


def init_db_for_path(db_path: str) -> None:
    """Initialize the database at an explicit path (used by CLI tools)."""
    import pathlib

    path = pathlib.Path(str(db_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _schema(conn)
    finally:
        conn.close()


def _schema(conn: sqlite3.Connection) -> None:
    """Create core tables only."""

    # --- AUTH ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_seeds (
            seed_id TEXT PRIMARY KEY DEFAULT 'master',
            seed_hash TEXT NOT NULL,
            role TEXT DEFAULT 'admin',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS node_seeds (
            node_name TEXT PRIMARY KEY,
            seed_hash TEXT NOT NULL,
            role TEXT DEFAULT 'worker',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS node_tokens (
            token_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            node_name TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            token_type TEXT DEFAULT 'runtime',
            pending BOOLEAN DEFAULT 0,
            role TEXT DEFAULT 'worker',
            expires_at TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # --- HUMAN USERS & RBAC ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            password_hash TEXT NOT NULL,
            is_active BOOLEAN DEFAULT 1,
            force_password_change BOOLEAN DEFAULT 1,
            created_at TEXT NOT NULL,
            created_by TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY,
            group_name TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_groups (
            user_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            PRIMARY KEY (user_id, group_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS permissions (
            permission_id TEXT PRIMARY KEY,
            permission_name TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_permissions (
            group_id TEXT NOT NULL,
            permission_id TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            PRIMARY KEY (group_id, permission_id),
            FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE,
            FOREIGN KEY (permission_id) REFERENCES permissions(permission_id) ON DELETE CASCADE
        )
    """)

    # --- DISCOVERY ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            node_name TEXT UNIQUE NOT NULL,
            endpoint TEXT,
            capabilities TEXT,
            load REAL DEFAULT 0.0,
            queue_depth INTEGER DEFAULT 0,
            available BOOLEAN DEFAULT 1,
            last_seen TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            role TEXT DEFAULT 'worker',
            first_heartbeat_seen BOOLEAN DEFAULT 0,
            registration_secret_hash TEXT,
            registration_secret_expires_at TEXT
        )
    """)

    # Normalized capability index (T-026). The legacy ``nodes.capabilities``
    # TEXT column keeps the full JSON payload (type, description, config,
    # input_schema, …) for the discovery API. This table stores only the
    # high-cardinality fields needed for efficient capability matching so
    # the scheduler can claim stages without ``json.loads`` on every node.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS node_capabilities (
            node_id TEXT NOT NULL,
            capability_name TEXT NOT NULL,
            capability_type TEXT,
            capability_version TEXT DEFAULT '1.0.0',
            description TEXT,
            input_schema TEXT,
            available BOOLEAN DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (node_id, capability_name),
            FOREIGN KEY (node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_capabilities_name "
        "ON node_capabilities(capability_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_capabilities_name_type "
        "ON node_capabilities(capability_name, capability_type)"
    )


    # --- PRESENCE ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS presence (
            node_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'online',
            mood TEXT,
            activity_json TEXT,
            progress INTEGER DEFAULT 0,
            eta_seconds INTEGER,
            next_available TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (node_id) REFERENCES nodes(node_id)
        )
    """)

    # --- TASKS ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            task_name TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            priority INTEGER DEFAULT 0,
            owner_node_id TEXT,
            timeout_seconds INTEGER DEFAULT 300,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (owner_node_id) REFERENCES nodes(node_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_stages (
            stage_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            stage_name TEXT NOT NULL,
            capability TEXT NOT NULL,
            depends_on TEXT,
            status TEXT DEFAULT 'pending',
            sequence INTEGER DEFAULT 0,
            timeout_seconds INTEGER DEFAULT 300,
            payload TEXT,
            result TEXT,
            claimed_by TEXT,
            claimed_at TEXT,
            claim_expires_at TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id),
            FOREIGN KEY (claimed_by) REFERENCES nodes(node_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            task_id TEXT,
            stage_id TEXT,
            name TEXT NOT NULL,
            mime_type TEXT,
            size_bytes INTEGER,
            checksum TEXT,
            storage_path TEXT NOT NULL,
            created_by TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (created_by) REFERENCES nodes(node_id)
        )
    """)

    # --- TASK NOTES (T-052) ---
    # Nodes can leave free-form text notes on a task while it is being
    # worked on (mini-chat between collaborating nodes). Notes are
    # ordered by created_at; deleting a task cascades to its notes.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_notes_task_id ON task_notes(task_id)"
    )

    # --- AUDIT LOGGING ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            log_id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL,
            actor_name TEXT,
            action TEXT NOT NULL,
            resource_type TEXT,
            resource_id TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_id)"
    )

    # --- INDEXES ---
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_capabilities ON nodes(capabilities)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_stages_task ON task_stages(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_stages_status ON task_stages(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_stages_capability ON task_stages(capability)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_presence_status ON presence(status)")

    # --- RBAC DEFAULTS ---
    _seed_default_rbac(conn)

    # --- MIGRATIONS ---
    _run_migrations(conn)

    conn.commit()


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Run lightweight schema migrations that add columns when missing."""
    # Ensure force_password_change column exists in users table.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "force_password_change" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN force_password_change BOOLEAN DEFAULT 1")
    # Ensure registration_secret_hash column exists in nodes table.
    cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
    if "registration_secret_hash" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN registration_secret_hash TEXT")
    if "registration_secret_expires_at" not in cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN registration_secret_expires_at TEXT")

    # Ensure token_lookup_hash column exists in node_tokens table (C-1 fix:
    # deterministic HMAC-SHA256 lookup replaces the O(N) bcrypt scan).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(node_tokens)").fetchall()]
    if "token_lookup_hash" not in cols:
        conn.execute("ALTER TABLE node_tokens ADD COLUMN token_lookup_hash TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_node_tokens_lookup ON node_tokens(token_lookup_hash)"
    )

    # Ensure audit_logs table exists (migration for existing databases).
    table_names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    if "audit_logs" not in table_names:
        conn.execute("""
            CREATE TABLE audit_logs (
                log_id TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL,
                actor_name TEXT,
                action TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                details TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_id)"
        )

    # T-052: ensure task_notes table exists (migration for existing
    # databases created before this table was added).
    if "task_notes" not in table_names:
        conn.execute("""
            CREATE TABLE task_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_notes_task_id ON task_notes(task_id)"
        )

    # T-060: ensure task_stages has the retry_count column (migration for
    # existing databases). The scheduler increments this counter each
    # time a claim is released back to pending, and fails the stage once
    # it exceeds settings.max_retries.
    ts_cols = [r[1] for r in conn.execute("PRAGMA table_info(task_stages)").fetchall()]
    if "retry_count" not in ts_cols:
        conn.execute(
            "ALTER TABLE task_stages ADD COLUMN retry_count INTEGER DEFAULT 0"
        )

    # T-053: ensure node_capabilities has the description and input_schema
    # columns (migration for existing databases).
    if "node_capabilities" in table_names:
        nc_cols = [r[1] for r in conn.execute("PRAGMA table_info(node_capabilities)").fetchall()]
        if "description" not in nc_cols:
            conn.execute(
                "ALTER TABLE node_capabilities ADD COLUMN description TEXT"
            )
        if "input_schema" not in nc_cols:
            conn.execute(
                "ALTER TABLE node_capabilities ADD COLUMN input_schema TEXT"
            )

    # T-026: backfill node_capabilities from the legacy JSON column for
    # existing databases. Runs once when the table is empty but nodes exist.
    _migrate_node_capabilities(conn)


def _migrate_node_capabilities(conn: sqlite3.Connection) -> None:
    """Populate ``node_capabilities`` from ``nodes.capabilities`` JSON.

    Idempotent: only inserts rows that do not already exist. Safe to run
    on every startup.
    """
    import json

    # Skip if the table doesn't exist yet (shouldn't happen because
    # _schema() creates it, but guard anyway).
    table_names = [
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    if "node_capabilities" not in table_names:
        return

    rows = conn.execute("SELECT node_id, capabilities FROM nodes").fetchall()
    now = datetime.now(timezone.utc).isoformat()
    for row in rows:
        node_id = row["node_id"]
        try:
            caps = json.loads(row["capabilities"]) if row["capabilities"] else []
        except Exception:
            caps = []
        for cap in caps:
            if not isinstance(cap, dict):
                # Capability given as a plain string -> use it as the name.
                name = str(cap)
                cap_type = None
                version = "1.0.0"
                available = 1
                description = None
                input_schema = None
            else:
                name = cap.get("name")
                if not name:
                    continue
                cap_type = cap.get("type")
                version = cap.get("version", "1.0.0")
                available = 1 if cap.get("available", True) else 0
                description = cap.get("description")
                schema = cap.get("input_schema")
                input_schema = json.dumps(schema) if schema is not None else None
            conn.execute(
                """
                INSERT INTO node_capabilities
                (node_id, capability_name, capability_type, capability_version,
                 description, input_schema, available, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, capability_name) DO UPDATE SET
                    capability_type = excluded.capability_type,
                    capability_version = excluded.capability_version,
                    description = excluded.description,
                    input_schema = excluded.input_schema,
                    available = excluded.available,
                    updated_at = excluded.updated_at
                """,
                (node_id, name, cap_type, version, description, input_schema, available, now),
            )


def _seed_default_rbac(conn: sqlite3.Connection) -> None:
    """Seed default groups and permissions if none exist."""
    now = datetime.now(timezone.utc).isoformat()

    # Default groups.
    default_groups = [
        ("grp_admin", "admin", "Full system access", now),
        ("grp_user", "user", "Standard user with limited access", now),
        ("grp_viewer", "viewer", "Read-only access", now),
    ]
    for group_id, group_name, description, created_at in default_groups:
        conn.execute(
            """
            INSERT INTO groups (group_id, group_name, description, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET group_name=excluded.group_name
            """,
            (group_id, group_name, description, created_at),
        )

    # Default permissions.
    default_permissions = [
        ("perm_dashboard", "dashboard:view", "Access the web dashboard", now),
        ("perm_nodes_view", "nodes:view", "View nodes", now),
        ("perm_nodes_approve", "nodes:approve", "Approve pending nodes", now),
        ("perm_nodes_token", "nodes:token", "Issue runtime tokens for approved nodes", now),
        ("perm_nodes_delete", "nodes:delete", "Delete nodes", now),
        ("perm_tasks_create", "tasks:create", "Create tasks", now),
        ("perm_tasks_view", "tasks:view", "View tasks", now),
        ("perm_tasks_admin", "tasks:admin", "Administer any task", now),
        ("perm_users_manage", "users:manage", "Manage human users", now),
        ("perm_groups_manage", "groups:manage", "Manage groups and permissions", now),
        ("perm_system_config", "system:config", "Change system configuration", now),
    ]
    for perm_id, perm_name, description, created_at in default_permissions:
        conn.execute(
            """
            INSERT INTO permissions (permission_id, permission_name, description, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(permission_id) DO UPDATE SET permission_name=excluded.permission_name
            """,
            (perm_id, perm_name, description, created_at),
        )

    # Admin group gets all permissions.
    admin_group_id = "grp_admin"
    for perm_id, _, _, _ in default_permissions:
        conn.execute(
            """
            INSERT INTO group_permissions (group_id, permission_id, granted_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id, permission_id) DO NOTHING
            """,
            (admin_group_id, perm_id, now),
        )

    # User group gets dashboard, view and task create permissions.
    user_group_id = "grp_user"
    user_permissions = ["perm_dashboard", "perm_nodes_view", "perm_tasks_view", "perm_tasks_create"]
    for perm_id in user_permissions:
        conn.execute(
            """
            INSERT INTO group_permissions (group_id, permission_id, granted_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id, permission_id) DO NOTHING
            """,
            (user_group_id, perm_id, now),
        )

    # Viewer group gets read-only permissions.
    viewer_group_id = "grp_viewer"
    viewer_permissions = ["perm_dashboard", "perm_nodes_view", "perm_tasks_view"]
    for perm_id in viewer_permissions:
        conn.execute(
            """
            INSERT INTO group_permissions (group_id, permission_id, granted_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id, permission_id) DO NOTHING
            """,
            (viewer_group_id, perm_id, now),
        )


@retry_on_locked
def log_audit_event(
    actor_id: str,
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    details: Optional[str] = None,
    actor_name: Optional[str] = None,
) -> None:
    """Write an audit log entry for an admin action.

    The ``details`` string is scanned for known secret patterns (tokens,
    registration secrets, bearer headers, ``password=`` / ``secret=``
    key-value pairs) and any matches are replaced with ``[REDACTED]``
    before being persisted. This is defense-in-depth: callers should
    never put raw secrets in ``details`` to begin with.
    """
    conn = get_conn()
    try:
        log_id = f"aud_{secrets.token_urlsafe(12)}"
        now = datetime.now(timezone.utc).isoformat()
        safe_details = _redact_secrets(details)
        conn.execute(
            """
            INSERT INTO audit_logs (log_id, actor_id, actor_name, action,
                                    resource_type, resource_id, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (log_id, actor_id, actor_name, action,
             resource_type, resource_id, safe_details, now),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Normalized capability index helpers (T-026)
# ---------------------------------------------------------------------------


@retry_on_locked
def sync_node_capabilities(node_id: str, capabilities: list) -> None:
    """Replace the ``node_capabilities`` rows for ``node_id``.

    Called whenever a node's capabilities change (registration,
    heartbeat with ``replace_capabilities``, approval, admin update).
    The full JSON payload continues to live in ``nodes.capabilities``;
    this helper keeps the normalized index in sync so the scheduler can
    match stages without ``json.loads`` on every node.
    """
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "DELETE FROM node_capabilities WHERE node_id = ?", (node_id,)
        )
        for cap in capabilities:
            if isinstance(cap, dict):
                name = cap.get("name")
                if not name:
                    continue
                cap_type = cap.get("type")
                version = cap.get("version", "1.0.0")
                available = 1 if cap.get("available", True) else 0
                description = cap.get("description")
                schema = cap.get("input_schema")
                input_schema = json.dumps(schema) if schema is not None else None
            else:
                name = str(cap)
                cap_type = None
                version = "1.0.0"
                available = 1
                description = None
                input_schema = None
            conn.execute(
                """
                INSERT INTO node_capabilities
                (node_id, capability_name, capability_type, capability_version,
                 description, input_schema, available, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (node_id, name, cap_type, version, description, input_schema, available, now),
            )
        conn.commit()
    finally:
        conn.close()


def get_node_capability_names(node_id: str) -> list[str]:
    """Return the capability names advertised by ``node_id``.

    Uses the normalized ``node_capabilities`` index instead of
    ``json.loads(nodes.capabilities)``. Returns an empty list if the
    node is unknown or has no capabilities.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT capability_name FROM node_capabilities WHERE node_id = ?",
            (node_id,),
        ).fetchall()
        return [r["capability_name"] for r in rows]
    finally:
        conn.close()


def nodes_with_capability(
    capability_name: str,
    capability_type: Optional[str] = None,
    statuses: tuple[str, ...] = ("approved", "online"),
) -> list[str]:
    """Return node_ids that advertise ``capability_name``.

    Efficient indexed lookup over ``node_capabilities`` joined to
    ``nodes``. ``statuses`` filters the node status (defaults to
    approved/online). Used by the scheduler's ``claim_stage``.
    """
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    params: list = [capability_name]
    sql = (
        "SELECT nc.node_id FROM node_capabilities nc "
        "JOIN nodes n ON n.node_id = nc.node_id "
        f"WHERE nc.capability_name = ? AND n.status IN ({placeholders})"
    )
    if capability_type is not None:
        sql += " AND nc.capability_type = ?"
        params.append(capability_type)
    params.extend(statuses)
    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [r["node_id"] for r in rows]
    finally:
        conn.close()


def get_capability_details(
    capability_name: str,
    node_id: Optional[str] = None,
) -> Optional[dict]:
    """Resolve the metadata for a single capability.

    Looks up ``description``, ``type`` and ``input_schema`` for the given
    capability name. When ``node_id`` is given the lookup is restricted
    to that node's row, otherwise the first row advertising the
    capability is used.

    Returns ``None`` when the capability is unknown. ``input_schema`` is
    parsed from JSON; if parsing fails it is returned as ``None``.
    """
    import json as _json

    if node_id is not None:
        sql = (
            "SELECT capability_name, capability_type, description, input_schema "
            "FROM node_capabilities WHERE node_id = ? AND capability_name = ?"
        )
        params: tuple = (node_id, capability_name)
    else:
        sql = (
            "SELECT capability_name, capability_type, description, input_schema "
            "FROM node_capabilities WHERE capability_name = ? "
            "ORDER BY description DESC, input_schema DESC LIMIT 1"
        )
        params = (capability_name,)
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        schema_raw = row["input_schema"]
        try:
            schema = _json.loads(schema_raw) if schema_raw else None
        except Exception:
            schema = None
        return {
            "name": row["capability_name"],
            "type": row["capability_type"],
            "description": row["description"] or "",
            "input_schema": schema,
        }
    finally:
        conn.close()
