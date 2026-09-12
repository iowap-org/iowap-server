"""T-192 regression: node delete vs. completed-stage FK history (F-REV-1).

complete_stage (core/scheduler.py) never clears a stage's claim columns —
a completed stage keeps claimed_by/claimed_at/claim_expires_at as
completion history. task_stages.claimed_by has a foreign key to
nodes(node_id) with no CASCADE, and the F-19 helper is claimed-only
(D-12), so without D-13 the admin node delete raises an IntegrityError
(HTTP 500) for every node with completed stage history.

D-13: the admin handler clears the remaining (non-claimed) node
references on task_stages after the F-19 helper ran — no status change,
no events. This test pins that contract.

Fixture pattern follows tests/test_artifact_observability.py (fresh_db
autouse fixture, TestClient, RELAY_DB_PATH env reset). Node and stage
rows are seeded via direct DB insert (claimed_by has a foreign key to
nodes — the node row must exist first).
"""

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.api.v2.auth import limiter as auth_limiter
from relay_server.config import settings
from relay_server.core.auth import generate_secret, hash_secret
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test and reset the event-bus history."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        settings.db_path = db_path
        settings.heartbeat_interval_seconds = 1
        settings.heartbeat_timeout_multiplier = 1
        auth_limiter.reset()
        event_bus.clear()
        init_db()
        yield Path(tmp)
        event_bus.clear()


client = TestClient(app)


def _seed_admin() -> str:
    secret = generate_secret("adm_")
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO admin_seeds (seed_id, seed_hash, role, created_at) VALUES (?, ?, ?, ?)",
            ("master", hash_secret(secret), "admin", "2026-01-01T00:00:00+00:00"),
        )
    )
    conn.commit()
    conn.close()
    return secret


def _register_admin(secret: str) -> tuple[str, str]:
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "Admin T192-reg",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    return body["node_id"], body["token"]


def _seed_node_row(node_id: str) -> None:
    """Insert a node row directly (claimed_by has an FK to nodes)."""
    now = datetime.now(UTC).isoformat()
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, capabilities, load, "
            "queue_depth, available, last_seen, registered_at, status, role) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (node_id, f"Node {node_id}", "[]", 0.0, 0, 1, now, now, "online", "worker"),
        )
    )
    conn.commit()
    conn.close()


def _seed_task_with_completed_stage(task_id: str, stage_id: str, node_id: str) -> None:
    """Seed a running task + one COMPLETED stage whose claim columns are
    still set — exactly the row state complete_stage leaves behind."""
    now_dt = datetime.now(UTC)
    now = now_dt.isoformat()
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO tasks (task_id, task_name, status, priority, "
            "timeout_seconds, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, "T192-reg task", "running", 0, 300, now, now),
        )
    )
    conn.execute(
        q(
            "INSERT INTO task_stages (stage_id, task_id, stage_name, "
            "capability, depends_on, status, sequence, timeout_seconds, "
            "payload, claimed_by, claimed_at, claim_expires_at, completed_at, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stage_id, task_id, "stage-0", "probe.cap", "[]",
                "completed", 0, 300, "{}",
                node_id, now, (now_dt + timedelta(hours=1)).isoformat(), now,
                now, now,
            ),
        ),
    )
    conn.commit()
    conn.close()


def _stage_row(stage_id: str):
    conn = get_conn()
    row = conn.execute(
        q("SELECT * FROM task_stages WHERE stage_id = ?", (stage_id,))
    ).fetchone()
    conn.close()
    return row


def _task_status(task_id: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        q("SELECT status FROM tasks WHERE task_id = ?", (task_id,))
    ).fetchone()
    conn.close()
    return row["status"] if row else None


def _events_of_type(etype: str) -> list[dict]:
    return [e for e in event_bus.recent(200) if e.get("type") == etype]


def test_node_delete_with_completed_stage_history():
    """F-REV-1 regression: deleting a node with completed stage history
    (claim columns still set on the completed rows) must succeed, keep the
    completed stage intact with cleared claim columns, and fire no events."""
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)

    _seed_node_row("n-reg")
    _seed_task_with_completed_stage("task_reg", "stage_reg_done", "n-reg")

    r = client.delete(
        "/relay/v2/admin/nodes/n-reg",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text

    # The node row is gone (D-13 cleared the dangling FK references).
    conn = get_conn()
    node_row = conn.execute(
        q("SELECT node_id FROM nodes WHERE node_id = ?", ("n-reg",))
    ).fetchone()
    conn.close()
    assert node_row is None, "F-REV-1: node delete must remove the node row"

    # The completed stage survives as completion history: status and
    # completed_at untouched, claim columns cleared (D-13).
    row = _stage_row("stage_reg_done")
    assert row["status"] == "completed", (
        "F-REV-1: completed stage must stay completed on node delete, "
        f"got {row['status']!r}"
    )
    assert row["claimed_by"] is None, (
        "D-13: claim columns must be cleared on surviving completed rows"
    )
    assert row["claimed_at"] is None
    assert row["claim_expires_at"] is None
    assert row["completed_at"] is not None

    # No failure cascade: the task (only completed stages) stays running.
    assert _task_status("task_reg") == "running"

    # No events for these rows: nothing failed, nothing changed status.
    assert _events_of_type("stage_failed") == []
    assert _events_of_type("task_failed") == []
    assert _events_of_type("status_changed") == []