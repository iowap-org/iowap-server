"""T-249: admin node delete must clear the node's forward_slots rows.

Before T-249 ``admin_delete_node`` (api/v2/admin.py) cleaned node_tokens,
presence, forward_mappings, tasks/stages/artifacts — but **not**
``forward_slots``. That table carries an FK to nodes (core/db.py
``forward_slots`` definition); app connections run with foreign_keys ON,
so deleting a node whose slots were still declared failed with
IntegrityError (HTTP 500) and the dashboard alert came up as
"Delete failed". Live hit (2026-10-25): the T-245 probe node
``t245-probe-throwaway`` could not be deleted from the dashboard.

Contract: DELETE /relay/v2/admin/nodes/{node_id} removes the node's
forward_slots rows BEFORE removing the node itself; the response shape
and the rest of the cleanup are unchanged.
"""

import json
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.core.auth import generate_secret, hash_secret
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        from relay_server.config import settings
        settings.db_path = Path(tmp) / "t249.db"
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


client = TestClient(app)


def _admin_token() -> str:
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
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "Admin T-249",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _node_with_slots(admin_token: str, name: str, slots: list[dict]) -> str:
    """Register + approve a node, then declare forward slots via heartbeat."""
    r = client.post(
        "/relay/v2/auth/register",
        json={
            "node_name": name,
            "endpoint": "http://localhost:9100",
            "capabilities": [{"name": "submit.misc", "version": "1.0.0"}],
            "role": "service",
        },
    )
    assert r.status_code == 200, r.text
    node_id = r.json()["node_id"]
    r = client.post(
        f"/relay/v2/admin/nodes/{node_id}/approve",
        json={"role": "service", "capabilities": [{"name": "submit.misc", "version": "1.0.0"}]},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    hb = client.post(
        "/relay/v2/discovery/worker-heartbeat",
        json={"forward_slots": slots},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert hb.status_code == 200, hb.text
    return node_id


def test_delete_node_clears_forward_slots_rows():
    admin_token = _admin_token()
    node_id = _node_with_slots(
        admin_token, "t249-probe", [{"slot": "1", "type": "text", "value": "probe"}]
    )
    conn = get_conn()
    before = conn.execute(
        q("SELECT COUNT(*) FROM forward_slots WHERE node_id = ?", (node_id,))
    ).fetchone()[0]
    conn.close()
    assert before == 1, "probe: slot declared before delete"

    r = client.delete(
        f"/relay/v2/admin/nodes/{node_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text  # pre-fix: IntegrityError → 500

    conn = get_conn()
    assert conn.execute(q("SELECT COUNT(*) FROM nodes WHERE node_id = ?", (node_id,))).fetchone()[0] == 0
    assert conn.execute(q("SELECT COUNT(*) FROM forward_slots WHERE node_id = ?", (node_id,))).fetchone()[0] == 0
    conn.close()


def test_delete_node_without_slots_still_works():
    admin_token = _admin_token()
    node_id = _node_with_slots(admin_token, "t249-plain", [])
    r = client.delete(
        f"/relay/v2/admin/nodes/{node_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    conn = get_conn()
    assert conn.execute(q("SELECT COUNT(*) FROM nodes WHERE node_id = ?", (node_id,))).fetchone()[0] == 0
    conn.close()


def test_delete_live_probe_node_after_declared_slots():
    """The live failure shape exactly: approved node, typed slot row, delete."""
    admin_token = _admin_token()
    node_id = _node_with_slots(
        admin_token, "t245-probe-throwaway",
        [{"slot": "1", "label": "T-245 Live-Beweis", "type": "text", "value": '"probe"'}],
    )
    r = client.delete(
        f"/relay/v2/admin/nodes/{node_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text