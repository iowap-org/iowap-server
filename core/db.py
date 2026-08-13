"""Database layer — pluggable backend abstraction with SQLite default.

The relay server talks to its database through a :class:`Database` protocol.
The active backend is selected by ``settings.db_type`` and instantiated once
via :func:`create_database`. Business logic calls ``db.get_conn()`` on the
module-level singleton ``db`` (or, for backward compatibility, the
module-level :func:`get_conn` / :func:`init_db` wrappers which delegate to it).

Currently supported backends:

* ``sqlite``  (default, fully implemented — :class:`SqliteDatabase`)
* ``postgres`` (implemented + live-tested — :class:`PostgresDatabase` in
  ``db_postgres.py``, SQLAlchemy Core engine with connection pooling; DSN
  ``postgresql+psycopg://user:pass@host:5432/relay`` via ``settings.pg_dsn``,
  T-110)
* ``mariadb`` (stub, raises ``NotImplementedError`` — :class:`MariadbDatabase`)

T-110: ``SqliteDatabase`` is now backed by a SQLAlchemy Core engine. The
on-disk SQLite database is the same file; SQLAlchemy simply opens it through
its sqlite3 DBAPI driver. A small compatibility shim is installed so the
existing 373 ``row["col"]`` access sites keep working unchanged (SQLAlchemy
2.0 ``Row`` supports ``row._mapping["col"]`` / ``row.col`` / ``row[0]`` but
not ``row["col"]`` directly).
"""

import functools
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Optional, Union

import sqlalchemy as sa
from sqlalchemy.engine.row import Row

from relay_server.config import settings

# A DB connection can be a raw sqlite3.Connection (used by CLI helpers and
# the backcompat test fixture) or a SQLAlchemy Connection (the normal path).
# The schema/migration/seed helpers accept either; _exec() ducks between them.
DBConn = Union["sqlite3.Connection", "sa.engine.Connection"]


# ---------------------------------------------------------------------------
# Row["col"] compatibility shim (T-110)
# ---------------------------------------------------------------------------
#
# SQLAlchemy 2.0 ``Row`` exposes columns via ``row.col``, ``row._mapping[col]``
# and ``row[index]`` but NOT via ``row["col"]`` (string subscript). The legacy
# sqlite3.Row interface does. Hundreds of call sites in the relay use
# ``row["col"]``; rather than rewriting all of them we install a tiny shim
# that forwards string subscripts to ``row._mapping[col]``. Integer
# subscripts keep their tuple semantics. The shim is idempotent and only
# installed once per process.
#
# NOTE (Kimi-Review 2026-08-04): this monkeypatch relies on SQLAlchemy 2.0's
# ``Row.__getitem__`` being a plain Python method descriptor. SQLAlchemy 2.1+
# may switch it to a C-extension slot that is not monkeypatchable. When
# upgrading, verify ``Row["col"]`` still works; if not, migrate the 373+
# ``row["col"]`` call sites to ``row._mapping["col"]`` instead.

_orig_row_getitem = Row.__getitem__


def _compat_row_getitem(self, key):
    if isinstance(key, str):
        return self._mapping[key]
    return _orig_row_getitem(self, key)


if not getattr(Row.__getitem__, "_relay_compat", False):
    Row.__getitem__ = _compat_row_getitem
    _compat_row_getitem._relay_compat = True  # type: ignore[attr-defined]


# sqlite3.Row exposes a ``keys()`` method returning the column names of the
# current row. SQLAlchemy 2.0 ``Row`` does not — callers must use
# ``row._mapping.keys()``. The legacy ``description in row.keys()`` pattern in
# discovery.py relies on this, so we install a small compatibility method.
if "keys" not in Row.__dict__ or not getattr(Row.__dict__.get("keys"), "_relay_compat", False):
    def _compat_row_keys(self):
        return self._mapping.keys()

    Row.keys = _compat_row_keys  # type: ignore[method-assign]
    _compat_row_keys._relay_compat = True  # type: ignore[attr-defined]

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
# Database protocol
# ---------------------------------------------------------------------------


class Database:
    """Pluggable database backend for the relay server.

    Each backend implements this interface. Business logic only ever calls
    :meth:`get_conn` (to obtain a connection) and :meth:`init_db` (to create
    schema + run migrations on startup). :meth:`close` releases resources
    (pools, file handles) on shutdown.
    """

    def get_conn(self) -> Any:
        """Return a usable database connection (sync)."""
        raise NotImplementedError

    def init_db(self) -> None:
        """Create schema, seed defaults, and run migrations. Idempotent."""
        raise NotImplementedError

    def close(self) -> None:
        """Release resources (pools, connections). No-op by default."""
        pass


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
            except (sqlite3.OperationalError, sa.exc.OperationalError) as exc:
                msg = str(exc)
                if "database is locked" not in msg and "locked" not in msg:
                    raise
                last_error = exc
                if attempt < LOCKED_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
        raise last_error  # type: ignore[misc]

    return wrapper


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


class SqliteDatabase(Database):
    """SQLite backend — the default.

    Backed by a SQLAlchemy Core engine (T-110) pointing at the on-disk
    SQLite file. The engine is built lazily on first use and rebuilt when
    ``settings.db_path`` changes, so tests that reconfigure the path per
    test keep working. Connections are short-lived (callers open and close
    them per operation); WAL mode and foreign keys are enabled on each
    open via the engine's connect event.
    """

    def __init__(self, db_path: Optional[Any] = None):
        # ``db_path`` is accepted for symmetry with the other backends but
        # unused: SqliteDatabase always reads settings.db_path live so the
        # test harness can re-point it per test.
        self._db_path = db_path
        self._engine: Optional[sa.Engine] = None
        self._engine_path: Optional[str] = None

    def _get_engine(self) -> sa.Engine:
        """Return an engine bound to the current ``settings.db_path``.

        The engine is cached by path; when the path changes (e.g. a test
        reconfigures it) a fresh engine is built and the old one disposed.
        """
        path = str(settings.db_path)
        if self._engine is None or self._engine_path != path:
            if self._engine is not None:
                self._engine.dispose()
            settings.db_path.parent.mkdir(parents=True, exist_ok=True)
            engine = sa.create_engine(
                f"sqlite:///{path}",
                connect_args={"check_same_thread": False},
                future=True,
            )

            @sa.event.listens_for(engine, "connect")
            def _set_sqlite_pragma(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

            self._engine = engine
            self._engine_path = path
        return self._engine

    def get_conn(self) -> sa.engine.Connection:
        """Open a fresh SQLAlchemy connection to the SQLite file."""
        return self._get_engine().connect()

    def init_db(self) -> None:
        """Initialize core tables for the relay server."""
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.get_conn()
        try:
            _schema(conn)
        finally:
            conn.close()

    def close(self) -> None:
        """Dispose the engine and release any pooled connections."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._engine_path = None


def init_db_for_path(db_path: str) -> None:
    """Initialize the database at an explicit path (used by CLI tools).

    Bypasses the active backend singleton and opens SQLite directly at the
    given path. This is a convenience for the ``relay-recovery`` CLI which
    operates on arbitrary database files.
    """
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


# ---------------------------------------------------------------------------
# Module-level singleton + factory
# ---------------------------------------------------------------------------
#
# ``db`` is the active backend instance shared across the process.
# :func:`create_database` builds it from ``settings.db_type``. The module-level
# :func:`get_conn` and :func:`init_db` delegate to ``db`` so callers that still
# import them directly (and tests) keep working. Tests that reconfigure
# ``settings.db_path`` per-test rely on :class:`SqliteDatabase.get_conn`
# reading settings live.


def create_database() -> Database:
    """Build the active backend from ``settings.db_type``."""
    db_type = settings.db_type
    if db_type == "sqlite":
        return SqliteDatabase()
    if db_type == "postgres":
        from relay_server.core.db_postgres import PostgresDatabase

        return PostgresDatabase(settings.pg_dsn)
    if db_type == "mariadb":
        from relay_server.core.db_mariadb import MariadbDatabase

        return MariadbDatabase(settings.mariadb_dsn)
    raise ValueError(f"Unknown db_type: {db_type!r}")


# Active backend singleton. Instantiated lazily on first import so that
# ``settings`` (which is built from YAML + env at import time) is ready.
db: Database = create_database()


def get_conn() -> Any:
    """Module-level accessor — delegates to the active backend ``db``.

    Kept for backward compatibility. New code should use ``db.get_conn()``
    directly via ``from relay_server.core.db import db``.
    """
    return db.get_conn()


def init_db() -> None:
    """Module-level accessor — delegates to the active backend ``db``."""
    db.init_db()


def _exec(conn, sql: str, params: Any = ()):
    """Execute a raw SQL string on either a SA Connection or sqlite3.Connection.

    DDL statements (CREATE/ALTER/PRAGMA, no ``?`` placeholders) run through
    ``exec_driver_sql`` on SQLAlchemy connections so they reach the DBAPI
    driver unchanged — this is fine for SQLite and used for the schema.

    Statements **with** ``?`` placeholders (seeds, migration DML) must go
    through :func:`q` so SQLAlchemy rewrites ``?`` to the active dialect's
    placeholder (``$N`` on PostgreSQL). ``exec_driver_sql`` would leak the
    raw ``?`` to Postgres and fail, so we route parameterised SQL through
    ``q()`` regardless of connection type.

    Raw sqlite3 connections expose ``execute(sql, params)`` directly.
    """
    has_params = params not in ((), None)
    if has_params:
        if hasattr(conn, "exec_driver_sql"):
            # SQLAlchemy Connection -> portable q() (rewrites ? to dialect)
            return conn.execute(q(sql, params))
        # Raw sqlite3.Connection -> execute accepts string + ? tuple directly
        return conn.execute(sql, params)
    # DDL / no params -> driver-level exec (avoids SA text() overhead)
    if hasattr(conn, "exec_driver_sql"):
        return conn.exec_driver_sql(sql)
    return conn.execute(sql)


def q(sql: str, params: Any = ()) -> "sa.TextClause":
    """Build a database-independent SQLAlchemy ``text()`` from ``?``-SQL.

    The legacy call sites use SQLite's ``?`` positional placeholders with a
    tuple of parameters: ``conn.execute("SELECT ... WHERE id = ?", (id,))``.
    SQLAlchemy 2.0 ``Connection.execute`` only accepts ``text()`` or Core
    constructs, and the placeholders must be dialect-portable. This helper
    rewrites ``?`` to named bind parameters (``:p0``, ``:p1`` …) and binds
    the tuple values in order. SQLAlchemy then renders the correct
    placeholder for the active dialect — ``?`` on SQLite, ``$N`` on
    PostgreSQL, ``%s`` on MySQL/MariaDB — so the same call site works
    against every backend.

    Usage::

        # before (SQLite-only):
        conn.execute("SELECT a FROM t WHERE id = ?", (id,))
        # after (portable):
        conn.execute(q("SELECT a FROM t WHERE id = ?", (id,)))

    For statements with no parameters the tuple may be omitted::

        conn.execute(q("SELECT 1"))
    """
    names: list[str] = []
    out: list[str] = []
    i = 0
    for ch in sql:
        if ch == "?":
            name = f"p{i}"
            names.append(name)
            out.append(f":{name}")
            i += 1
        else:
            out.append(ch)
    text = sa.text("".join(out))
    if params:
        mapping = dict(zip(names, params))
        text = text.bindparams(**mapping)
    return text


def _schema(conn: DBConn) -> None:
    """Create core tables only."""

    # --- AUTH ---
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS admin_seeds (
            seed_id TEXT PRIMARY KEY DEFAULT 'master',
            seed_hash TEXT NOT NULL,
            role TEXT DEFAULT 'admin',
            created_at TEXT NOT NULL
        )
    """)
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS node_seeds (
            node_name TEXT PRIMARY KEY,
            seed_hash TEXT NOT NULL,
            role TEXT DEFAULT 'worker',
            created_at TEXT NOT NULL
        )
    """)
    _exec(conn, """
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
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            password_hash TEXT NOT NULL,
            is_active BOOLEAN DEFAULT 1,
            force_password_change BOOLEAN DEFAULT 1,
            created_at TEXT NOT NULL,
            created_by TEXT,
            status TEXT DEFAULT 'active'
        )
    """)
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS groups (
            group_id TEXT PRIMARY KEY,
            group_name TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        )
    """)
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS user_groups (
            user_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            granted_at TEXT NOT NULL,
            PRIMARY KEY (user_id, group_id),
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES groups(group_id) ON DELETE CASCADE
        )
    """)
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS permissions (
            permission_id TEXT PRIMARY KEY,
            permission_name TEXT UNIQUE NOT NULL,
            description TEXT,
            created_at TEXT NOT NULL
        )
    """)
    _exec(conn, """
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
    _exec(conn, """
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
            registration_secret_expires_at TEXT,
            description TEXT,
            consecutive_high_load INTEGER DEFAULT 0
        )
    """)

    # Normalized capability index (T-026). The legacy ``nodes.capabilities``
    # TEXT column keeps the full JSON payload (type, description, config,
    # input_schema, …) for the discovery API. This table stores only the
    # high-cardinality fields needed for efficient capability matching so
    # the scheduler can claim stages without ``json.loads`` on every node.
    _exec(conn, """
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
    _exec(conn, 
        "CREATE INDEX IF NOT EXISTS idx_node_capabilities_name "
        "ON node_capabilities(capability_name)"
    )
    _exec(conn, 
        "CREATE INDEX IF NOT EXISTS idx_node_capabilities_name_type "
        "ON node_capabilities(capability_name, capability_type)"
    )

    # T-075: dynamic node routes — API endpoints declared by nodes in their
    # capability YAML and registered via heartbeat.
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS node_routes (
            node_id TEXT NOT NULL,
            path TEXT NOT NULL,
            method TEXT NOT NULL,
            auth TEXT NOT NULL DEFAULT 'session',
            upstream TEXT NOT NULL,
            description TEXT DEFAULT '',
            PRIMARY KEY (node_id, path, method),
            FOREIGN KEY (node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        )
    """)


    # --- PRESENCE ---
    _exec(conn, """
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
    _exec(conn, """
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

    _exec(conn, """
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

    _exec(conn, """
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
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS task_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
        )
    """)
    _exec(conn, 
        "CREATE INDEX IF NOT EXISTS idx_task_notes_task_id ON task_notes(task_id)"
    )

    # --- AUDIT LOGGING ---
    _exec(conn, """
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
    _exec(conn, 
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at)"
    )
    _exec(conn, 
        "CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_id)"
    )

    # T-164/T-165: settings_override — DB-persisted overrides for
    # transfer-ladder + artifact-TTL config. The YAML/env values remain
    # the defaults; rows here override them at runtime so the dashboard
    # can edit them without rewriting config.yaml. (key, value) with key
    # in {max_inline_bytes, max_artifact_bytes, artifact_ttl_days}.
    _exec(conn, """
        CREATE TABLE IF NOT EXISTS settings_override (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            updated_by TEXT
        )
    """)

    # --- INDEXES ---
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_nodes_capabilities ON nodes(capabilities)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_task_stages_task ON task_stages(task_id)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_task_stages_status ON task_stages(status)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_task_stages_capability ON task_stages(capability)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id)")
    _exec(conn, "CREATE INDEX IF NOT EXISTS idx_presence_status ON presence(status)")

    # --- RBAC DEFAULTS ---
    _seed_default_rbac(conn)

    # --- MIGRATIONS ---
    _run_migrations(conn)

    conn.commit()


def _run_migrations(conn: DBConn) -> None:
    """Run lightweight schema migrations that add columns when missing.

    Backend-aware: ``PRAGMA table_info`` is used on SQLite, and
    ``information_schema`` is used on PostgreSQL/other backends. The
    column-name introspection is centralised in :func:`_column_names` and
    the table listing in :func:`_table_names`.
    """
    # Ensure force_password_change column exists in users table.
    cols = _column_names(conn, "users")
    if "force_password_change" not in cols:
        _exec(conn, "ALTER TABLE users ADD COLUMN force_password_change BOOLEAN DEFAULT 1")
    # Ensure registration_secret_hash column exists in nodes table.
    cols = _column_names(conn, "nodes")
    if "registration_secret_hash" not in cols:
        _exec(conn, "ALTER TABLE nodes ADD COLUMN registration_secret_hash TEXT")
    if "registration_secret_expires_at" not in cols:
        _exec(conn, "ALTER TABLE nodes ADD COLUMN registration_secret_expires_at TEXT")
    # T-072: ensure nodes has the description column (node-level prose,
    # set per heartbeat by the node itself).
    if "description" not in cols:
        _exec(conn, "ALTER TABLE nodes ADD COLUMN description TEXT")

    # Ensure token_lookup_hash column exists in node_tokens table (C-1 fix:
    # deterministic HMAC-SHA256 lookup replaces the O(N) bcrypt scan).
    cols = _column_names(conn, "node_tokens")
    if "token_lookup_hash" not in cols:
        _exec(conn, "ALTER TABLE node_tokens ADD COLUMN token_lookup_hash TEXT")
    _exec(conn,
        "CREATE INDEX IF NOT EXISTS idx_node_tokens_lookup ON node_tokens(token_lookup_hash)"
    )

    # Ensure audit_logs table exists (migration for existing databases).
    table_names = _table_names(conn)
    if "audit_logs" not in table_names:
        _exec(conn, """
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
        _exec(conn,
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at)"
        )
        _exec(conn,
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_id)"
        )

    # T-052: ensure task_notes table exists (migration for existing
    # databases created before this table was added).
    if "task_notes" not in table_names:
        _exec(conn, """
            CREATE TABLE task_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                node_id TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
            )
        """)
        _exec(conn,
            "CREATE INDEX IF NOT EXISTS idx_task_notes_task_id ON task_notes(task_id)"
        )

    # T-154: task_notes kind column (longrun/progress/info). Additive;
    # existing notes default to 'info' (no semantic meaning for the relay).
    notes_cols = _column_names(conn, "task_notes")
    if "kind" not in notes_cols:
        _exec(conn,
            "ALTER TABLE task_notes ADD COLUMN kind TEXT DEFAULT 'info'"
        )

    # T-154: task_stages last_note_at + longrun_ttl_expires_at for the
    # Long-Run lease. claimed_by stays the owner; these track the note-based
    # heartbeat TTL instead of the static claimed_at+timeout_seconds.
    ts_cols2 = _column_names(conn, "task_stages")
    if "last_note_at" not in ts_cols2:
        _exec(conn,
            "ALTER TABLE task_stages ADD COLUMN last_note_at TEXT"
        )
    if "longrun_ttl_expires_at" not in ts_cols2:
        _exec(conn,
            "ALTER TABLE task_stages ADD COLUMN longrun_ttl_expires_at TEXT"
        )

    # T-060: ensure task_stages has the retry_count column (migration for
    # existing databases). The scheduler increments this counter each
    # time a claim is released back to pending, and fails the stage once
    # it exceeds settings.max_retries.
    ts_cols = _column_names(conn, "task_stages")
    if "retry_count" not in ts_cols:
        _exec(conn,
            "ALTER TABLE task_stages ADD COLUMN retry_count INTEGER DEFAULT 0"
        )

    # T-079 / T-085: users.status column. The legacy ``is_active`` boolean
    # is kept for backward compatibility; the new ``status`` text column
    # carries the canonical status ("active" / "inactive") from the central
    # status registry (core/status.py). Existing rows are backfilled from
    # ``is_active`` when that column exists so an active user maps to
    # "active" and a deactivated one to "inactive".
    user_cols = _column_names(conn, "users")
    if "status" not in user_cols:
        _exec(conn, "ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'active'")
        # Backfill from is_active for any pre-existing rows, but only
        # when the legacy column is present (some very old databases
        # predate is_active entirely).
        if "is_active" in user_cols:
            _exec(conn,
                "UPDATE users SET status = CASE WHEN is_active = 0 THEN 'inactive' "
                "ELSE 'active' END WHERE status IS NULL"
            )

    # T-081: nodes.consecutive_high_load — counter used by the auto-busy
    # logic in discovery.mark_offline_nodes(). A node whose load stays at
    # or above its load_cap for ``consecutive_high_load`` heartbeats in a
    # row is automatically transitioned to "busy"; the counter resets to
    # 0 whenever the load drops back below the cap.
    node_cols = _column_names(conn, "nodes")
    if "consecutive_high_load" not in node_cols:
        _exec(conn,
            "ALTER TABLE nodes ADD COLUMN consecutive_high_load INTEGER DEFAULT 0"
        )

    # T-053: ensure node_capabilities has the description and input_schema
    # columns (migration for existing databases).
    if "node_capabilities" in table_names:
        nc_cols = _column_names(conn, "node_capabilities")
        if "description" not in nc_cols:
            _exec(conn,
                "ALTER TABLE node_capabilities ADD COLUMN description TEXT"
            )
        if "input_schema" not in nc_cols:
            _exec(conn,
                "ALTER TABLE node_capabilities ADD COLUMN input_schema TEXT"
            )
        # T-164: upload_modes — JSON-Array der unterstützten Übertragungsmodi
        # (inline / artifact / bridge). Nullable; Default beim Lesen ist die
        # volle Treppe [inline, artifact, bridge].
        if "upload_modes" not in nc_cols:
            _exec(conn,
                "ALTER TABLE node_capabilities ADD COLUMN upload_modes TEXT"
            )

    # T-123: ensure node_routes has the expires_at + channel_id columns
    # (migration for existing databases). Both are nullable so existing
    # permanent heartbeat routes (expires_at IS NULL) keep working
    # unchanged. New temp bridge routes register with a TTL via T-124.
    if "node_routes" in table_names:
        nr_cols = _column_names(conn, "node_routes")
        if "expires_at" not in nr_cols:
            _exec(conn,
                "ALTER TABLE node_routes ADD COLUMN expires_at TEXT"
            )
        if "channel_id" not in nr_cols:
            _exec(conn,
                "ALTER TABLE node_routes ADD COLUMN channel_id TEXT"
            )
        # Index the channel_id for the temp-route lookup path so a node
        # can resolve a route by channel without a full scan.
        _exec(conn,
            "CREATE INDEX IF NOT EXISTS idx_node_routes_channel "
            "ON node_routes(channel_id) WHERE channel_id IS NOT NULL"
        )

    # T-026: backfill node_capabilities from the legacy JSON column for
    # existing databases. Runs once when the table is empty but nodes exist.
    _migrate_node_capabilities(conn)


def _column_names(conn, table: str) -> list[str]:
    """Return the column names of ``table`` in declaration order.

    Uses ``PRAGMA table_info`` on SQLite (where it is the canonical
    introspection) and ``information_schema.columns`` on other backends.
    Handles raw ``sqlite3.Connection`` (used by the backcompat test fixture
    and CLI helpers) as well as SQLAlchemy ``Connection`` objects.
    """
    if _is_sqlite(conn):
        # Raw sqlite3.Connection has .execute(sql, params); SA Connection has
        # .exec_driver_sql(sql, params). Both accept the plain PRAGMA string.
        rows = _exec(conn, f"PRAGMA table_info({table})").fetchall()
        return [r[1] for r in rows]
    # Portable path: information_schema.columns (PostgreSQL, others).
    # Use sa.text() (not exec_driver_sql) so the :t bind param is rendered
    # in the active dialect ($1 on Postgres); exec_driver_sql would leak :t.
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t ORDER BY ordinal_position"
        ),
        {"t": table},
    ).fetchall()
    return [r[0] for r in rows]


def _table_names(conn) -> list[str]:
    """Return the names of all tables in the current database.

    Uses ``sqlite_master`` on SQLite and ``information_schema.tables`` on
    other backends.
    """
    if _is_sqlite(conn):
        rows = _exec(conn, "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [r[0] for r in rows]
    rows = conn.exec_driver_sql(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ).fetchall()
    return [r[0] for r in rows]


def _is_sqlite(conn) -> bool:
    """Heuristically detect a SQLite connection (SA or raw sqlite3)."""
    dialect = getattr(conn, "dialect", None)
    if dialect is not None:
        return dialect.name == "sqlite"
    # Raw sqlite3.Connection — check the module / type.
    mod = type(conn).__module__
    return "sqlite3" in mod or isinstance(conn, sqlite3.Connection)


def _migrate_node_capabilities(conn: DBConn) -> None:
    """Populate ``node_capabilities`` from ``nodes.capabilities`` JSON.

    Idempotent: only inserts rows that do not already exist. Safe to run
    on every startup.
    """
    import json

    # Skip if the table doesn't exist yet (shouldn't happen because
    # _schema() creates it, but guard anyway).
    table_names = _table_names(conn)
    if "node_capabilities" not in table_names:
        return

    rows = _exec(conn, "SELECT node_id, capabilities FROM nodes").fetchall()
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
                modes = cap.get("upload_modes")
                upload_modes = json.dumps(modes) if modes is not None else None
            _exec(conn, 
                """
                INSERT INTO node_capabilities
                (node_id, capability_name, capability_type, capability_version,
                 description, input_schema, upload_modes, available, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, capability_name) DO UPDATE SET
                    capability_type = excluded.capability_type,
                    capability_version = excluded.capability_version,
                    description = excluded.description,
                    input_schema = excluded.input_schema,
                    upload_modes = excluded.upload_modes,
                    available = excluded.available,
                    updated_at = excluded.updated_at
                """,
                (node_id, name, cap_type, version, description, input_schema,
                 upload_modes, available, now),
            )


def _seed_default_rbac(conn: DBConn) -> None:
    """Seed default groups and permissions if none exist."""
    now = datetime.now(timezone.utc).isoformat()

    # Default groups.
    default_groups = [
        ("grp_admin", "admin", "Full system access", now),
        ("grp_user", "user", "Standard user with limited access", now),
        ("grp_viewer", "viewer", "Read-only access", now),
    ]
    for group_id, group_name, description, created_at in default_groups:
        _exec(conn, 
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
        _exec(conn, 
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
        _exec(conn, 
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
        _exec(conn, 
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
        _exec(conn, 
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
        _exec(conn, 
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
        _exec(conn, 
            "DELETE FROM node_capabilities WHERE node_id = ?", (node_id,)
        )
        for cap in capabilities:
            if isinstance(cap, dict):
                name = cap.get("name")
                if not name:
                    continue
                cap_type = cap.get("type")
                version = cap.get("version", "1.0.0")
                # T-066: cap.get("available", True) returns True when the key is
                # absent, but Pydantic may set available=None from the request body
                # and bool(None) is False. Only treat explicit False as unavailable.
                available = 1 if cap.get("available") is not False else 0
                description = cap.get("description")
                schema = cap.get("input_schema")
                input_schema = json.dumps(schema) if schema is not None else None
                # T-164: upload_modes — JSON-Array der Übertragungsmodi.
                modes = cap.get("upload_modes")
                upload_modes = json.dumps(modes) if modes is not None else None
            else:
                name = str(cap)
                cap_type = None
                version = "1.0.0"
                available = 1
                description = None
                input_schema = None
                upload_modes = None
            _exec(conn, 
                """
                INSERT INTO node_capabilities
                (node_id, capability_name, capability_type, capability_version,
                 description, input_schema, upload_modes, available, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (node_id, name, cap_type, version, description, input_schema,
                 upload_modes, available, now),
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
        rows = _exec(conn, 
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
        rows = _exec(conn, sql, tuple(params)).fetchall()
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

    T-164: ``upload_modes`` (inline / artifact / bridge) wird mitgeliefert,
    damit die node-cli bei ``file send``/``file get`` den passenden
    Übertragungsmodus wählen kann. Default ist die volle Treppe, wenn die
    Spalte null ist.
    """
    import json as _json

    if node_id is not None:
        sql = (
            "SELECT capability_name, capability_type, description, input_schema, upload_modes "
            "FROM node_capabilities WHERE node_id = ? AND capability_name = ?"
        )
        params: tuple = (node_id, capability_name)
    else:
        sql = (
            "SELECT capability_name, capability_type, description, input_schema, upload_modes "
            "FROM node_capabilities WHERE capability_name = ? "
            "ORDER BY description DESC, input_schema DESC LIMIT 1"
        )
        params = (capability_name,)
    conn = get_conn()
    try:
        row = _exec(conn, sql, params).fetchone()
        if not row:
            return None
        schema_raw = row["input_schema"]
        try:
            schema = _json.loads(schema_raw) if schema_raw else None
        except Exception:
            schema = None
        # T-164: upload_modes — Default volle Treppe.
        upload_modes = ["inline", "artifact", "bridge"]
        modes_raw = row["upload_modes"] if "upload_modes" in row.keys() else None
        if modes_raw:
            try:
                parsed = _json.loads(modes_raw)
                if isinstance(parsed, list):
                    upload_modes = parsed
            except Exception:
                pass
        return {
            "name": row["capability_name"],
            "type": row["capability_type"],
            "description": row["description"] or "",
            "input_schema": schema,
            "upload_modes": upload_modes,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# settings_override — DB-persisted config overrides (T-164/T-165)
# ---------------------------------------------------------------------------

# Keys that the dashboard is allowed to override. The ladder constraints
# (inline < artifact, inline × 1.4 < payload, ttl ≥ 1) are validated by
# the setter, not by the schema.
_OVERRIDABLE_KEYS = {"max_inline_bytes", "max_artifact_bytes", "artifact_ttl_days"}


def get_settings_overrides() -> dict[str, str]:
    """Return all ``settings_override`` rows as a ``{key: value}`` dict."""
    conn = get_conn()
    try:
        rows = _exec(conn, "SELECT key, value FROM settings_override").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def set_settings_override(key: str, value: str, updated_by: Optional[str] = None) -> None:
    """Insert or update a single ``settings_override`` row.

    Only keys in :data:`_OVERRIDABLE_KEYS` are accepted; unknown keys
    raise ``ValueError``. The caller is responsible for type-coercing
    ``value`` before passing it in (we store the canonical string form).
    """
    if key not in _OVERRIDABLE_KEYS:
        raise ValueError(f"setting {key!r} is not overridable")
    conn = get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        _exec(conn,
            """
            INSERT INTO settings_override (key, value, updated_at, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (key, str(value), now, updated_by),
        )
        conn.commit()
    finally:
        conn.close()


def clear_settings_override(key: str) -> bool:
    """Delete a single ``settings_override`` row. Returns True if a row was removed."""
    if key not in _OVERRIDABLE_KEYS:
        raise ValueError(f"setting {key!r} is not overridable")
    conn = get_conn()
    try:
        deleted = _exec(conn, "DELETE FROM settings_override WHERE key = ?", (key,)).rowcount
        conn.commit()
        return bool(deleted)
    finally:
        conn.close()


def apply_settings_overrides() -> None:
    """Apply DB overrides onto the live ``settings`` object.

    Called at startup (after ``init_db``) and after each dashboard edit.
    Coerces the stored string back to the field's declared type and
    re-validates the ladder constraints via the Pydantic validators.
    """
    overrides = get_settings_overrides()
    if not overrides:
        return

    # Build a merged model_dump from the current settings, apply overrides,
    # then re-construct Settings so the field_validators run again.
    current = settings.model_dump()
    for key, raw in overrides.items():
        if key not in current:
            continue
        # Coerce based on the declared type of the default value.
        default = current[key]
        if isinstance(default, bool):
            current[key] = str(raw).lower() in ("1", "true", "yes")
        elif isinstance(default, int):
            try:
                current[key] = int(raw)
            except ValueError:
                continue
        elif isinstance(default, float):
            try:
                current[key] = float(raw)
            except ValueError:
                continue
        else:
            current[key] = raw
    # Re-validate through the model so the ladder validators fire.
    from relay_server.config import Settings

    try:
        merged = Settings(**current)
    except Exception:  # noqa: BLE001 — invalid override skipped, keep old
        return
    # Mutate the live singleton in place.
    for key, val in merged.model_dump().items():
        setattr(settings, key, val)
