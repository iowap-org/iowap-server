"""T-245: targeted claims need capability info in the SSE payload.

Server side of the pair (iowap-node T-245):
- ``task_created`` publishes ``capabilities`` — the sorted, deduped
  capability set of the task's stages (T-016 linear-DAG decomposition).
- ``stage_claimed`` publishes ``capability`` — the claimed stage's
  capability, so a node seeing the broadcast can attempt a targeted
  claim instead of a cap-blind broadside sweep.

Measured live on the mesh (2026-11-02, felix SSE-Daemon): a cap-blind
post-completion broadside over N capabilities takes N × (0.2–1.4 s) —
11–12 s for felix (7 caps), projected 5–30 s for NovaForge (~23 caps).
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from fastapi.testclient import TestClient

from relay_server.config import settings
from relay_server.core.auth import generate_secret, hash_secret
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        settings.db_path = db_path
        from relay_server.api.v2.auth import limiter as auth_limiter

        auth_limiter.reset()
        event_bus.clear()
        init_db()
        yield
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
            "node_name": "Admin T245",
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


def _events_of_type(etype: str) -> list:
    return [e for e in event_bus.recent(200) if e.get("type") == etype]


def test_task_created_payload_lists_stage_capabilities_sorted_deduped():
    from relay_server.core.scheduler import Scheduler

    Scheduler.create_task(
        "t245 multi",
        [
            {"stage_name": "s1", "capability": "build"},
            {"stage_name": "s2", "capability": "index"},
            {"stage_name": "s3", "capability": "build"},  # duplicate → dedupe
        ],
    )
    events = _events_of_type("task_created")
    assert events, "no task_created event published"
    payload = events[-1]["payload"]
    assert payload["capabilities"] == ["build", "index"]


def test_task_created_payload_single_stage():
    from relay_server.core.scheduler import Scheduler

    Scheduler.create_task(
        "t245 single", [{"stage_name": "s1", "capability": "build"}]
    )
    payload = _events_of_type("task_created")[-1]["payload"]
    assert payload["capabilities"] == ["build"]


def test_stage_claimed_payload_carries_capability():
    secret = _seed_admin()
    admin_token = _register_admin(secret)[1]
    worker_id, _pending = _register_worker(
        "t245-worker", [{"name": "build", "version": "1.0.0"}]
    )
    runtime = _approve(
        admin_token, worker_id, [{"name": "build", "version": "1.0.0"}]
    )
    _hb(runtime)
    _set_node_online(worker_id)

    from relay_server.core.scheduler import Scheduler

    Scheduler.create_task(
        "t245 claim", [{"stage_name": "s1", "capability": "build"}]
    )
    assert Scheduler.claim_stage(worker_id, capability="build") is not None

    events = _events_of_type("stage_claimed")
    assert events, "no stage_claimed event published"
    payload = events[-1]["payload"]
    assert payload["capability"] == "build"
    assert "stage_id" in payload and "task_id" in payload


def _set_node_online(worker_id: str) -> None:
    """Direct DB transition to online (T-080 pattern) — the claim gate
    needs node_can_claim(status) to pass."""
    conn = get_conn()
    conn.execute(
        q("UPDATE nodes SET status = 'online' WHERE node_id = ?", (worker_id,))
    )
    conn.commit()
    conn.close()