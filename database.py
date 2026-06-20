"""Database layer — SQLite connection pool and schema management."""

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from relay_server.config import settings


DB = settings.db_path


def get_conn() -> sqlite3.Connection:
    """Create a fresh database connection with WAL mode and Row factory."""
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Initialize all tables for the relay server."""
    conn = get_conn()
    try:
        _schema(conn)
        _seed_defaults(conn)
    finally:
        conn.close()


def _schema(conn: sqlite3.Connection) -> None:
    """Create tables."""

    # --- AUTH ---
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
            role TEXT DEFAULT 'worker',
            expires_at TEXT,
            created_at TEXT NOT NULL
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
            status TEXT DEFAULT 'online'
        )
    """)

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
            type TEXT DEFAULT 'atomic',
            description TEXT,
            status TEXT DEFAULT 'queued',
            priority TEXT DEFAULT 'normal',
            requirements_json TEXT,
            payload_json TEXT,
            result_json TEXT,
            assigned_node TEXT,
            claim_expires TEXT,
            created_at TEXT NOT NULL,
            timeout_seconds INTEGER DEFAULT 300,
            retries INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 2,
            parent_task_id TEXT,
            stage_index INTEGER,
            FOREIGN KEY (assigned_node) REFERENCES nodes(node_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_stages (
            stage_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            capability TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            assigned_node TEXT,
            payload_json TEXT,
            result_json TEXT,
            depends_on TEXT,
            inputs_json TEXT,
            outputs_json TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id),
            FOREIGN KEY (assigned_node) REFERENCES nodes(node_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            task_id TEXT,
            stage_id TEXT,
            node_id TEXT,
            type TEXT,
            path TEXT,
            size_bytes INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id),
            FOREIGN KEY (stage_id) REFERENCES task_stages(stage_id),
            FOREIGN KEY (node_id) REFERENCES nodes(node_id)
        )
    """)

    # --- BOARD ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            subject TEXT,
            machine_json TEXT,
            human_markdown TEXT,
            visibility TEXT DEFAULT 'public',
            parent_id TEXT,
            thread_root TEXT,
            edited_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (node_id) REFERENCES nodes(node_id),
            FOREIGN KEY (parent_id) REFERENCES posts(post_id),
            FOREIGN KEY (thread_root) REFERENCES posts(post_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS reactions (
            post_id TEXT,
            node_id TEXT,
            emoji TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (post_id, node_id, emoji),
            FOREIGN KEY (post_id) REFERENCES posts(post_id)
        )
    """)

    # --- VAULT ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vault_pages (
            page_id TEXT PRIMARY KEY,
            scope TEXT DEFAULT 'node:local',
            path TEXT UNIQUE NOT NULL,
            slug TEXT UNIQUE NOT NULL,
            title TEXT,
            type TEXT DEFAULT 'note',
            tags TEXT,
            body TEXT DEFAULT '',
            frontmatter TEXT,
            node_id TEXT DEFAULT '',
            version INTEGER DEFAULT 1,
            lint_status TEXT DEFAULT 'unlinted',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            checksum TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS vault_links (
            source_page_id TEXT NOT NULL,
            target_slug TEXT NOT NULL,
            context TEXT DEFAULT '',
            PRIMARY KEY (source_page_id, target_slug)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS vault_versions (
            page_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            body TEXT,
            frontmatter TEXT,
            node_id TEXT DEFAULT '',
            saved_at TEXT NOT NULL,
            PRIMARY KEY (page_id, version)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS vault_aliases (
            alias TEXT PRIMARY KEY,
            canonical TEXT NOT NULL,
            source_node TEXT DEFAULT '',
            scope TEXT DEFAULT 'cluster:shared'
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS vault_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS vault_log (
            log_id TEXT PRIMARY KEY,
            page_id TEXT,
            action TEXT NOT NULL,
            node_id TEXT,
            detail TEXT,
            timestamp TEXT NOT NULL
        )
    """)

    # --- ACTIVITY ---
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activities (
            activity_id TEXT PRIMARY KEY,
            node_id TEXT NOT NULL,
            type TEXT NOT NULL,
            machine_json TEXT,
            human TEXT,
            visibility TEXT DEFAULT 'public',
            created_at TEXT NOT NULL,
            FOREIGN KEY (node_id) REFERENCES nodes(node_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS follows (
            follower TEXT,
            following TEXT,
            notify_on TEXT,
            since TEXT NOT NULL,
            PRIMARY KEY (follower, following)
        )
    """)

    # --- INDEXES ---
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_capabilities ON nodes(capabilities)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_node)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_thread ON posts(thread_root)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_node ON posts(node_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_activities_node ON activities(node_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vault_scope ON vault_pages(scope)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vault_type ON vault_pages(type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vault_lint ON vault_pages(lint_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vault_links_target ON vault_links(target_slug)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vault_log_page ON vault_log(page_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_task_stages_task ON task_stages(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_presence_status ON presence(status)")

    conn.commit()


def _seed_defaults(conn: sqlite3.Connection) -> None:
    """Insert default config values."""
    defaults = [
        ("lint_cooldown_seconds", "600"),
        ("lint_threshold", "10"),
        ("lint_max_age_hours", "168"),
        ("lint_last_submitted", ""),
    ]
    for key, value in defaults:
        conn.execute(
            "INSERT OR IGNORE INTO vault_meta (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
