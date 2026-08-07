"""Portable schema definitions for the relay server (T-110).

Every table is declared once here as a :class:`sqlalchemy.Table` against a
shared :class:`sqlalchemy.MetaData`. ``metadata.create_all(engine)`` then
builds the schema on any backend (SQLite, PostgreSQL, MariaDB) from the
same declaration — no dialect-specific DDL strings in business code.

Conventions (deliberate, see T-110 plan):

* Timestamps are stored as ISO-8601 **TEXT** strings. SQLite already does
  this and the on-disk database must stay byte-identical. PostgreSQL can
  store TEXT just as well; a TIMESTAMPTZ migration is a later, separate
  step.
* No DDL defaults that call ``strftime(...)`` — timestamps are produced
  Python-side via ``datetime.now(timezone.utc).isoformat()`` on insert.
* ``sa.Boolean`` is used for boolean flags — both SQLite and PostgreSQL
  understand it (SQLite maps it to INTEGER 0/1, which matches the existing
  schema).
* The ``task_notes.id`` column uses ``sa.Integer`` with ``autoincrement=True``
  which resolves to ``INTEGER PRIMARY KEY AUTOINCREMENT`` on SQLite and a
  ``SERIAL``/identity on PostgreSQL.
"""

from __future__ import annotations

import sqlalchemy as sa

# Shared metadata — every table below is attached to this object.
# Callers do ``metadata.create_all(engine)`` to build the schema.
metadata = sa.MetaData()

# ---------------------------------------------------------------------------
# AUTH — seeds & tokens
# ---------------------------------------------------------------------------

admin_seeds = sa.Table(
    "admin_seeds", metadata,
    sa.Column("seed_id", sa.String(64), primary_key=True, default="master"),
    sa.Column("seed_hash", sa.String(255), nullable=False),
    sa.Column("role", sa.String(32), default="admin"),
    sa.Column("created_at", sa.String(64), nullable=False),
)

node_seeds = sa.Table(
    "node_seeds", metadata,
    sa.Column("node_name", sa.String(255), primary_key=True),
    sa.Column("seed_hash", sa.String(255), nullable=False),
    sa.Column("role", sa.String(32), default="worker"),
    sa.Column("created_at", sa.String(64), nullable=False),
)

node_tokens = sa.Table(
    "node_tokens", metadata,
    sa.Column("token_id", sa.String(64), primary_key=True),
    sa.Column("node_id", sa.String(64), nullable=False),
    sa.Column("node_name", sa.String(255), nullable=False),
    sa.Column("token_hash", sa.String(255), nullable=False, unique=True),
    sa.Column("token_type", sa.String(32), default="runtime"),
    sa.Column("pending", sa.Boolean, default=False),
    sa.Column("role", sa.String(32), default="worker"),
    sa.Column("expires_at", sa.String(64), nullable=True),
    sa.Column("created_at", sa.String(64), nullable=False),
    # Added by migration (C-1 fix): deterministic HMAC-SHA256 lookup hash.
    sa.Column("token_lookup_hash", sa.String(255), nullable=True),
)

# ---------------------------------------------------------------------------
# HUMAN USERS & RBAC
# ---------------------------------------------------------------------------

users = sa.Table(
    "users", metadata,
    sa.Column("user_id", sa.String(64), primary_key=True),
    sa.Column("username", sa.String(255), nullable=False, unique=True),
    sa.Column("email", sa.String(255), nullable=True),
    sa.Column("password_hash", sa.String(255), nullable=False),
    sa.Column("is_active", sa.Boolean, default=True),
    sa.Column("force_password_change", sa.Boolean, default=True),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("created_by", sa.String(64), nullable=True),
    sa.Column("status", sa.String(32), default="active"),
)

groups = sa.Table(
    "groups", metadata,
    sa.Column("group_id", sa.String(64), primary_key=True),
    sa.Column("group_name", sa.String(255), nullable=False, unique=True),
    sa.Column("description", sa.Text, nullable=True),
    sa.Column("created_at", sa.String(64), nullable=False),
)

user_groups = sa.Table(
    "user_groups", metadata,
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("group_id", sa.String(64), nullable=False),
    sa.Column("granted_at", sa.String(64), nullable=False),
    sa.PrimaryKeyConstraint("user_id", "group_id"),
    sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
    sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"], ondelete="CASCADE"),
)

permissions = sa.Table(
    "permissions", metadata,
    sa.Column("permission_id", sa.String(64), primary_key=True),
    sa.Column("permission_name", sa.String(255), nullable=False, unique=True),
    sa.Column("description", sa.Text, nullable=True),
    sa.Column("created_at", sa.String(64), nullable=False),
)

group_permissions = sa.Table(
    "group_permissions", metadata,
    sa.Column("group_id", sa.String(64), nullable=False),
    sa.Column("permission_id", sa.String(64), nullable=False),
    sa.Column("granted_at", sa.String(64), nullable=False),
    sa.PrimaryKeyConstraint("group_id", "permission_id"),
    sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"], ondelete="CASCADE"),
    sa.ForeignKeyConstraint(["permission_id"], ["permissions.permission_id"], ondelete="CASCADE"),
)

# ---------------------------------------------------------------------------
# DISCOVERY
# ---------------------------------------------------------------------------

nodes = sa.Table(
    "nodes", metadata,
    sa.Column("node_id", sa.String(64), primary_key=True),
    sa.Column("node_name", sa.String(255), nullable=False, unique=True),
    sa.Column("endpoint", sa.String(512), nullable=True),
    sa.Column("capabilities", sa.Text, nullable=True),
    sa.Column("load", sa.Float, default=0.0),
    sa.Column("queue_depth", sa.Integer, default=0),
    sa.Column("available", sa.Boolean, default=True),
    sa.Column("last_seen", sa.String(64), nullable=False),
    sa.Column("registered_at", sa.String(64), nullable=False),
    sa.Column("status", sa.String(32), default="pending"),
    sa.Column("role", sa.String(32), default="worker"),
    sa.Column("first_heartbeat_seen", sa.Boolean, default=False),
    # Added by migration (registration secrets).
    sa.Column("registration_secret_hash", sa.String(255), nullable=True),
    sa.Column("registration_secret_expires_at", sa.String(64), nullable=True),
    # T-072: node-level prose set per heartbeat by the node itself.
    sa.Column("description", sa.Text, nullable=True),
    # T-081: auto-busy counter.
    sa.Column("consecutive_high_load", sa.Integer, default=0),
)

# Normalized capability index (T-026).
node_capabilities = sa.Table(
    "node_capabilities", metadata,
    sa.Column("node_id", sa.String(64), nullable=False),
    sa.Column("capability_name", sa.String(255), nullable=False),
    sa.Column("capability_type", sa.String(255), nullable=True),
    sa.Column("capability_version", sa.String(64), default="1.0.0"),
    sa.Column("description", sa.Text, nullable=True),
    sa.Column("input_schema", sa.Text, nullable=True),
    sa.Column("available", sa.Boolean, default=True),
    sa.Column("updated_at", sa.String(64), nullable=False),
    sa.PrimaryKeyConstraint("node_id", "capability_name"),
    sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"], ondelete="CASCADE"),
)

# T-075: dynamic node routes — API endpoints declared by nodes.
# T-123: ``expires_at`` + ``channel_id`` carry temporary bridge routes.
# ``expires_at IS NULL``  → a permanent heartbeat route (replaced on every
# heartbeat via ``_sync_node_routes``). ``expires_at`` non-null → a
# temporary bridge route registered via ``POST /node-routes/register`` (T-124)
# with a TTL; it is reaped by ``temp_route_cleanup`` (T-125) once it expires.
# ``channel_id`` ties a temp route to the upload/download channel that
# created it and is required for temp routes (NULL for heartbeat routes).
node_routes = sa.Table(
    "node_routes", metadata,
    sa.Column("node_id", sa.String(64), nullable=False),
    sa.Column("path", sa.String(512), nullable=False),
    sa.Column("method", sa.String(16), nullable=False),
    sa.Column("auth", sa.String(32), nullable=False, default="session"),
    sa.Column("upstream", sa.String(512), nullable=False),
    sa.Column("description", sa.Text, default=""),
    sa.Column("expires_at", sa.String(64), nullable=True),
    sa.Column("channel_id", sa.String(64), nullable=True),
    sa.PrimaryKeyConstraint("node_id", "path", "method"),
    sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"], ondelete="CASCADE"),
)

# ---------------------------------------------------------------------------
# PRESENCE
# ---------------------------------------------------------------------------

presence = sa.Table(
    "presence", metadata,
    sa.Column("node_id", sa.String(64), primary_key=True),
    sa.Column("status", sa.String(32), default="online"),
    sa.Column("mood", sa.String(255), nullable=True),
    sa.Column("activity_json", sa.Text, nullable=True),
    sa.Column("progress", sa.Integer, default=0),
    sa.Column("eta_seconds", sa.Integer, nullable=True),
    sa.Column("next_available", sa.String(64), nullable=True),
    sa.Column("updated_at", sa.String(64), nullable=False),
    sa.ForeignKeyConstraint(["node_id"], ["nodes.node_id"]),
)

# ---------------------------------------------------------------------------
# TASKS
# ---------------------------------------------------------------------------

tasks = sa.Table(
    "tasks", metadata,
    sa.Column("task_id", sa.String(64), primary_key=True),
    sa.Column("task_name", sa.String(255), nullable=False),
    sa.Column("status", sa.String(32), default="pending"),
    sa.Column("priority", sa.Integer, default=0),
    sa.Column("owner_node_id", sa.String(64), nullable=True),
    sa.Column("timeout_seconds", sa.Integer, default=300),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("updated_at", sa.String(64), nullable=False),
    sa.Column("completed_at", sa.String(64), nullable=True),
    sa.ForeignKeyConstraint(["owner_node_id"], ["nodes.node_id"]),
)

task_stages = sa.Table(
    "task_stages", metadata,
    sa.Column("stage_id", sa.String(64), primary_key=True),
    sa.Column("task_id", sa.String(64), nullable=False),
    sa.Column("stage_name", sa.String(255), nullable=False),
    sa.Column("capability", sa.String(255), nullable=False),
    sa.Column("depends_on", sa.Text, nullable=True),
    sa.Column("status", sa.String(32), default="pending"),
    sa.Column("sequence", sa.Integer, default=0),
    sa.Column("timeout_seconds", sa.Integer, default=300),
    sa.Column("payload", sa.Text, nullable=True),
    sa.Column("result", sa.Text, nullable=True),
    sa.Column("claimed_by", sa.String(64), nullable=True),
    sa.Column("claimed_at", sa.String(64), nullable=True),
    sa.Column("claim_expires_at", sa.String(64), nullable=True),
    sa.Column("completed_at", sa.String(64), nullable=True),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("updated_at", sa.String(64), nullable=False),
    sa.Column("retry_count", sa.Integer, default=0),
    sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"]),
    sa.ForeignKeyConstraint(["claimed_by"], ["nodes.node_id"]),
)

artifacts = sa.Table(
    "artifacts", metadata,
    sa.Column("artifact_id", sa.String(64), primary_key=True),
    sa.Column("task_id", sa.String(64), nullable=True),
    sa.Column("stage_id", sa.String(64), nullable=True),
    sa.Column("name", sa.String(255), nullable=False),
    sa.Column("mime_type", sa.String(255), nullable=True),
    sa.Column("size_bytes", sa.Integer, nullable=True),
    sa.Column("checksum", sa.String(255), nullable=True),
    sa.Column("storage_path", sa.String(512), nullable=False),
    sa.Column("created_by", sa.String(64), nullable=True),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.ForeignKeyConstraint(["created_by"], ["nodes.node_id"]),
)

# --- TASK NOTES (T-052) ----------------------------------------------------
task_notes = sa.Table(
    "task_notes", metadata,
    # ``autoincrement=True`` resolves to INTEGER PRIMARY KEY AUTOINCREMENT on
    # SQLite and to an identity/serial column on PostgreSQL.
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("task_id", sa.String(64), nullable=False),
    sa.Column("node_id", sa.String(64), nullable=False),
    sa.Column("message", sa.Text, nullable=False),
    sa.Column("created_at", sa.String(64), nullable=True),
    sa.ForeignKeyConstraint(["task_id"], ["tasks.task_id"], ondelete="CASCADE"),
)

# ---------------------------------------------------------------------------
# AUDIT LOGGING
# ---------------------------------------------------------------------------

audit_logs = sa.Table(
    "audit_logs", metadata,
    sa.Column("log_id", sa.String(64), primary_key=True),
    sa.Column("actor_id", sa.String(64), nullable=False),
    sa.Column("actor_name", sa.String(255), nullable=True),
    sa.Column("action", sa.String(255), nullable=False),
    sa.Column("resource_type", sa.String(64), nullable=True),
    sa.Column("resource_id", sa.String(64), nullable=True),
    sa.Column("details", sa.Text, nullable=True),
    sa.Column("created_at", sa.String(64), nullable=False),
)

# ---------------------------------------------------------------------------
# INDEXES (portable — ``sa.Index`` resolves per dialect)
# ---------------------------------------------------------------------------

sa.Index("idx_node_capabilities_name", node_capabilities.c.capability_name)
sa.Index("idx_node_capabilities_name_type",
         node_capabilities.c.capability_name, node_capabilities.c.capability_type)
sa.Index("idx_task_notes_task_id", task_notes.c.task_id)
sa.Index("idx_audit_logs_created", audit_logs.c.created_at)
sa.Index("idx_audit_logs_actor", audit_logs.c.actor_id)
sa.Index("idx_nodes_status", nodes.c.status)
sa.Index("idx_nodes_capabilities", nodes.c.capabilities)
sa.Index("idx_tasks_status", tasks.c.status)
sa.Index("idx_tasks_priority", tasks.c.priority)
sa.Index("idx_task_stages_task", task_stages.c.task_id)
sa.Index("idx_task_stages_status", task_stages.c.status)
sa.Index("idx_task_stages_capability", task_stages.c.capability)
sa.Index("idx_artifacts_task", artifacts.c.task_id)
sa.Index("idx_presence_status", presence.c.status)
sa.Index("idx_node_tokens_lookup", node_tokens.c.token_lookup_hash)


def all_table_names() -> set[str]:
    """Return the set of table names declared on :data:`metadata`."""
    return set(metadata.tables.keys())