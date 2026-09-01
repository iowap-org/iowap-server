"""T-171: Discovery visibility must follow the status registry.

Regression for the ha-app-node outage (iowap-ha T-171): the node went
auto-busy (load_cap unit bug) and the hardcoded
``status IN ('approved','online')`` discovery filter made all of its
capabilities vanish. Busy — and the post-drain revert target ``idle`` —
must stay visible; claim eligibility is the scheduler's job
(``node_can_claim``), not discovery's.
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.config import settings
from relay_server.core.auth import generate_secret, hash_secret
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.status import node_claim_statuses, node_live_statuses
from relay_server.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database for each test and reset the event bus."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        settings.db_path = db_path
        settings.heartbeat_interval_seconds = 1
        settings.heartbeat_timeout_multiplier = 1
        init_db()
        yield


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
            "node_name": "Admin T171",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    return body["node_id"], body["token"]


def _register_worker(name: str, capabilities: list) -> tuple[str, str]:
    r = client.post(
        "/relay/v2/auth/register",
        json={
            "node_name": name,
            "endpoint": "http://localhost:9001",
            "capabilities": capabilities,
            "role": "service",
        },
    )
    assert r.status_code == 200
    body = r.json()
    return body["node_id"], body["token"]


def _approve(admin_token: str, worker_id: str, caps: list) -> str:
    r = client.post(
        f"/relay/v2/admin/nodes/{worker_id}/approve",
        json={"role": "service", "capabilities": caps},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    return r.json()["token"]


def _hb(runtime: str, **payload) -> None:
    r = client.post(
        "/relay/v2/discovery/heartbeat",
        headers={"Authorization": f"Bearer {runtime}"},
        json=payload,
    )
    assert r.status_code == 200


def _node_status(runtime: str, worker_id: str) -> str:
    r = client.get(
        "/relay/v2/discovery/nodes",
        headers={"Authorization": f"Bearer {runtime}"},
    )
    nodes = {n["node_id"]: n for n in r.json()["nodes"]}
    return nodes[worker_id]["status"]


def test_busy_and_idle_nodes_stay_visible_in_capability_query():
    """Auto-busy (queue_depth=1) and its revert target 'idle' must both
    keep the node in /discovery/query results (T-171)."""
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)
    worker_id, _ = _register_worker("Worker V", [{"name": "vault", "version": "1.0.0"}])
    runtime = _approve(admin_token, worker_id, [{"name": "vault", "version": "1.0.0"}])

    _hb(runtime, load=5.0, queue_depth=1, available=True)
    assert _node_status(runtime, worker_id) == "busy"

    r = client.get(
        "/relay/v2/discovery/query?capability=vault",
        headers={"Authorization": f"Bearer {runtime}"},
    )
    assert r.status_code == 200
    nodes = r.json()["nodes"]
    assert [n["node_id"] for n in nodes] == [worker_id], (
        "busy node must stay discoverable (T-171)"
    )

    # Queue drains → revert to idle → still visible.
    _hb(runtime, load=5.0, queue_depth=0, available=True)
    assert _node_status(runtime, worker_id) == "idle"

    r = client.get(
        "/relay/v2/discovery/query?capability=vault",
        headers={"Authorization": f"Bearer {runtime}"},
    )
    assert r.status_code == 200
    nodes = r.json()["nodes"]
    assert [n["node_id"] for n in nodes] == [worker_id], (
        "idle node must stay discoverable (T-171)"
    )


def test_node_live_statuses_include_busy_and_idle():
    live = set(node_live_statuses())
    assert {"online", "idle", "busy"} <= live
    # Claimable remains strictly narrower than visible.
    assert set(node_claim_statuses()) <= live
    assert "busy" not in node_claim_statuses()