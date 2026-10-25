"""T-248: the public node profile exposes sender + worker nodes for recent tasks.

Before T-248 the recent-tasks rows carried only task_id/task_name/status/
priority/created_at/completed_at (api/v2/cluster.py) — the dashboard task
table could not show WHO submitted a task and WHICH node executed it. With
forward routing and the typed slot series this attribution is the visible
half of the contract: the admin reads nodes, not IDs.

Fields added per recent-tasks row (all resolved to NAMES, not raw IDs):
- ``sender_node_id`` / ``sender_node_name`` — tasks.submitted_by (T-227;
  NULL legacy tasks carry ``None`` / "—").
- ``worker_node_ids`` / ``worker_node_names`` — the DISTINCT claimed_by
  values across the task's stages; falls back to ``owner_node_id`` when a
  task has no explicit claim yet.
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.main import app

PRO = "/relay/v2/cluster"


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "cluster.db"
        from relay_server.config import settings
        settings.db_path = db_path
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


client = TestClient(app)


def _seed_nodes() -> dict:
    """Three nodes + one task with sender/owner/claim relationships."""
    conn = get_conn()
    conn.execute(
        q("INSERT INTO nodes (node_id, node_name, status, capabilities, registered_at, last_seen)"
          " VALUES ('SENDER1', 'flow-runner', 'online', '[]', '2026-01-01T00:00:00+00:00',"
          " '2026-01-01T00:00:00+00:00')"))
    conn.execute(
        q("INSERT INTO nodes (node_id, node_name, status, capabilities, registered_at, last_seen)"
          " VALUES ('WORKER1', 'NovaForge', 'online', '[]', '2026-01-01T00:00:00+00:00',"
          " '2026-01-01T00:00:00+00:00')"))
    conn.execute(
        q("INSERT INTO nodes (node_id, node_name, status, capabilities, registered_at, last_seen)"
          " VALUES ('WORKER2', 'felix-cyberfox', 'online', '[]', '2026-01-01T00:00:00+00:00',"
          " '2026-01-01T00:00:00+00:00')"))
    conn.execute(
        q("INSERT INTO tasks (task_id, task_name, status, priority, owner_node_id, submitted_by,"
          " created_at, updated_at) VALUES ('task_T248a', 'probe-task', 'completed', 5,"
          " 'WORKER1', 'SENDER1', '2026-01-01T00:00:00+00:00',"
          " '2026-01-01T00:00:00+00:00')"))
    conn.execute(
        q("INSERT INTO task_stages (stage_id, task_id, stage_name, capability, status,"
          " sequence, timeout_seconds, payload, claimed_by, created_at, updated_at)"
          " VALUES ('stage_T248a', 'task_T248a', 'main', 'agent.ai', 'completed', 0, 300, '{}',"
          " 'WORKER1', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"))
    # Second claimer on the same task (fan-out: worker names must list BOTH).
    conn.execute(
        q("INSERT INTO task_stages (stage_id, task_id, stage_name, capability, status,"
          " sequence, timeout_seconds, payload, claimed_by, created_at, updated_at)"
          " VALUES ('stage_T248b', 'task_T248a', 'second', 'chat.ai', 'completed', 1, 300, '{}',"
          " 'WORKER2', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"))
    conn.commit()
    conn.close()
    return {"SENDER1": "flow-runner", "WORKER1": "NovaForge", "WORKER2": "felix-cyberfox"}


def _profile(node_id: str) -> dict:
    r = client.get(f"{PRO}/nodes/{node_id}")
    assert r.status_code == 200, r.text
    return r.json()


def test_recent_tasks_carry_sender_and_worker_names():
    _seed_nodes()
    prof = _profile("WORKER1")
    assert prof["recent_tasks"], "probe task must appear in WORKER1's recent list"
    row = prof["recent_tasks"][0]
    assert row["sender_node_id"] == "SENDER1"
    assert row["sender_node_name"] == "flow-runner"
    assert set(row["worker_node_ids"]) == {"WORKER1", "WORKER2"}
    assert row["worker_node_names"] == ["NovaForge", "felix-cyberfox"]


def test_recent_tasks_dedupe_repeated_claimer_names():
    _seed_nodes()
    # Fan-out with a repeated claimer: the name list must dedupe (sorted).
    conn = get_conn()
    conn.execute(
        q("INSERT INTO task_stages (stage_id, task_id, stage_name, capability, status,"
          " sequence, timeout_seconds, payload, claimed_by, created_at, updated_at)"
          " VALUES ('stage_T248d', 'task_T248a', 'third', 'terminal.ai', 'completed', 2, 300, '{}',"
          " 'WORKER1', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"))
    conn.commit()
    conn.close()
    prof = _profile("WORKER1")
    row = {t["task_id"]: t for t in prof["recent_tasks"]}["task_T248a"]
    assert row["worker_node_ids"] == ["WORKER1", "WORKER2"]
    assert row["worker_node_names"] == ["NovaForge", "felix-cyberfox"]


def test_legacy_task_without_sender_carries_null_sender():
    _seed_nodes()
    conn = get_conn()
    conn.execute(
        q("INSERT INTO tasks (task_id, task_name, status, priority, owner_node_id, submitted_by,"
          " created_at, updated_at) VALUES ('task_T248b', 'legacy-task', 'completed', 5,"
          " 'WORKER1', NULL, '2026-01-01T00:00:00+00:00',"
          " '2026-01-01T00:00:00+00:00')"))
    conn.execute(
        q("INSERT INTO task_stages (stage_id, task_id, stage_name, capability, status,"
          " sequence, timeout_seconds, payload, claimed_by, created_at, updated_at)"
          " VALUES ('stage_T248c', 'task_T248b', 'main', 'agent.ai', 'completed', 0, 300, '{}',"
          " 'WORKER1', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"))
    conn.commit()
    conn.close()
    prof = _profile("WORKER1")
    rows = {t["task_id"]: t for t in prof["recent_tasks"]}
    assert rows["task_T248b"]["sender_node_id"] is None
    assert rows["task_T248b"]["sender_node_name"] is None
    assert rows["task_T248b"]["worker_node_names"] == ["NovaForge"]


def test_unclaimed_task_falls_back_to_owner_name():
    _seed_nodes()
    conn = get_conn()
    conn.execute(
        q("INSERT INTO tasks (task_id, task_name, status, priority, owner_node_id, submitted_by,"
          " created_at, updated_at) VALUES ('task_T248c', 'pending-task', 'pending', 5,"
          " 'WORKER2', 'SENDER1', '2026-01-01T00:00:00+00:00',"
          " '2026-01-01T00:00:00+00:00')"))
    # No stage row at all: the worker list must resolve from owner_node_id.
    conn.commit()
    conn.close()
    prof = _profile("WORKER2")
    rows = {t["task_id"]: t for t in prof["recent_tasks"]}
    assert rows["task_T248c"]["worker_node_names"] == ["felix-cyberfox"]

# ── Admin overview (T-248): the 📋 Tasks table gets sender + worker columns ──

DB = "/relay/v2/dashboard"


def _legacy_and_pending_tasks() -> None:
    """Shared legacy/pending seeds for the null-safe overview + cluster checks."""
    conn = get_conn()
    conn.execute(
        q("INSERT INTO tasks (task_id, task_name, status, priority, owner_node_id, submitted_by,"
          " created_at, updated_at) VALUES ('task_T248b', 'legacy-task', 'completed', 5,"
          " 'WORKER1', NULL, '2026-01-01T00:00:00+00:00',"
          " '2026-01-01T00:00:00+00:00')"))
    conn.execute(
        q("INSERT INTO task_stages (stage_id, task_id, stage_name, capability, status,"
          " sequence, timeout_seconds, payload, claimed_by, created_at, updated_at)"
          " VALUES ('stage_T248c', 'task_T248b', 'main', 'agent.ai', 'completed', 0, 300, '{}',"
          " 'WORKER1', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"))
    conn.execute(
        q("INSERT INTO tasks (task_id, task_name, status, priority, owner_node_id, submitted_by,"
          " created_at, updated_at) VALUES ('task_T248c', 'pending-task', 'pending', 5,"
          " 'WORKER2', 'SENDER1', '2026-01-01T00:00:00+00:00',"
          " '2026-01-01T00:00:00+00:00')"))
    conn.commit()
    conn.close()


def _overview_tasks() -> list[dict]:
    from relay_server.core.session import sign_user_cookie
    client.cookies.set(
        "relay_user",
        sign_user_cookie({"user_id": "__master__", "username": "master"}),
        domain="testserver.local",
    )
    r = client.get(f"{DB}/api/overview")
    assert r.status_code == 200, r.text
    return r.json()["tasks"]


def test_admin_overview_tasks_carry_sender_and_worker_names():
    _seed_nodes()
    _legacy_and_pending_tasks()
    tasks = {t["task_id"]: t for t in _overview_tasks()}
    assert tasks["task_T248a"]["sender_node_name"] == "flow-runner"
    assert tasks["task_T248a"]["worker_node_names"] == ["NovaForge", "felix-cyberfox"]
    assert tasks["task_T248b"]["sender_node_name"] is None
    assert tasks["task_T248b"]["worker_node_names"] == ["NovaForge"]
    assert tasks["task_T248c"]["worker_node_names"] == ["felix-cyberfox"]
