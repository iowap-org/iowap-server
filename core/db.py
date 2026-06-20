"""Database layer — SQLite connection pool and core schema management."""

import sqlite3
from datetime import datetime, timezone

from relay_server.config import settings


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
            first_heartbeat_seen BOOLEAN DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status)")

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
            FOREIGN KEY (task_id) REFERENCES tasks(task_id),
            FOREIGN KEY (stage_id) REFERENCES task_stages(stage_id),
            FOREIGN KEY (created_by) REFERENCES nodes(node_id)
        )
    """)

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

    conn.commit()


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
