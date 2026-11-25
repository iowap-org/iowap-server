"""T-046/T-047-Rest verification tests — NO code change expected (green).

Both tests verify AS-BUILT behaviour against 2.0.3; a failure means the
node-repo contract broke, not the relay:

1. **Explicit busy honored (T-046 busy switch, relay side):** a
   worker-heartbeat WITH ``status: "busy"`` flips the node to busy — the
   T-081 explicit-request path in core/discovery.py (validated against
   the central registry, node_can_transition online→busy) — and a busy
   node gets NO claims (core/scheduler.py claim_stage gate: only
   AVAILABLE nodes claim, T-080). Toggle-off: the NEXT heartbeat with
   ``status: "idle"`` reverts the node (registry transition busy→idle)
   and claims flow again. This is exactly what the node's dashboard
   busy-toggle drives: it persists ``status`` in node.yaml and the
   daemon heartbeat forwards it every tick.
2. **2-Node neighbor E2E (T-047 rest, slots as the service side):**
   the NEIGHBOR node declares its own slots IN ITS HEARTBEAT (the
   wire-form of its node.yaml ``forward_slots`` — typed: "1" forward,
   "2" text/value), submits with (submitter, slot): the T-245 C5 gate
   refuses the VALUE slot before mapping (422 forward_slot_not_routable),
   the forward slot resolves through the admin-created MAPPING to the
   target capability, the stage is rewritten at the relay, and the
   TARGET node claims it (owner flips to the neighbor) — the full chain
   declaration → C5 gate → mapping → target_capability → claim/run at
   the target, over real routing paths (the T-247 unit tests cover the
   mechanics, this covers the chain).
"""

import json
import os
import tempfile
import urllib.parse
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

BASE = "/relay/v2/scheduler"


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test; reset limiter and event bus."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        settings.db_path = db_path
        # Pin the defaults so register/approve chains stay non-flaky.
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
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


def _register_admin(secret: str) -> str:
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "Admin T046",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200
    return r.json()["token"]


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


def _register_and_approve(admin_token: str, name: str, caps: list) -> tuple[str, str]:
    node_id, _pending = _register_worker(name, caps)
    token = _approve(admin_token, node_id, caps)
    return node_id, token


def _admin() -> str:
    return _register_admin(_seed_admin())


def _node_status(node_id: str) -> str:
    conn = get_conn()
    row = conn.execute(
        q("SELECT status FROM nodes WHERE node_id = ?", (node_id,))
    ).fetchone()
    conn.close()
    return row["status"]


def _heartbeat(token: str, body: dict) -> None:
    r = client.post(
        "/relay/v2/discovery/worker-heartbeat",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text


def _claim(token: str, capability: str) -> dict:
    r = client.post(
        BASE + "/claim",
        json={"capability": capability},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _put_mapping(admin_token: str, node_id: str, slot: str, target: str):
    return client.put(
        f"/relay/v2/forward-mappings/{node_id}/{urllib.parse.quote(slot, safe='')}",
        json={"target_capability": target},
        headers={"Authorization": f"Bearer {admin_token}"},
    )


def _stage_row(task_id: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        q(
            "SELECT capability, forward_slot, status FROM task_stages WHERE task_id = ?",
            (task_id,),
        )
    ).fetchone()
    conn.close()
    return row


def _task_owner(task_id: str) -> str:
    conn = get_conn()
    row = conn.execute(
        q("SELECT owner_node_id FROM tasks WHERE task_id = ?", (task_id,))
    ).fetchone()
    conn.close()
    return row["owner_node_id"]


# ---------------------------------------------------------------------------
# 1) Explicit busy honored — toggle-off reverts to idle
# ---------------------------------------------------------------------------


def test_explicit_busy_heartbeat_stops_claims_until_toggle_off():
    admin_token = _admin()
    caps = [{"name": "probe.echo", "version": "1.0.0"}]
    node_id, token = _register_and_approve(admin_token, "t046-busy-node", caps)

    # the node goes online via its heartbeat, then toggles busy ON
    _heartbeat(token, {"capabilities": caps, "load": 0.0, "queue_depth": 0})
    assert _node_status(node_id) == "online"
    _heartbeat(
        token,
        {"capabilities": caps, "load": 0.0, "queue_depth": 0, "status": "busy"},
    )
    assert (
        _node_status(node_id) == "busy"
    ), "the explicit busy request must flip the node (T-081 path)"

    # busy → NO claims (claim gate, T-080): nothing exists yet, but even a
    # pending stage must not reach a busy node — prove both directions.
    r = client.post(
        BASE + "/tasks",
        json={
            "task_name": "busy-block",
            "stages": [{"stage_name": "s1", "capability": "probe.echo", "payload": {}}],
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    claim_body = _claim(token, "probe.echo")
    assert claim_body["claimed"] is False, "a busy node must not claim stages"

    # toggle-off: the NEXT heartbeat carries status idle → busy→idle revert
    _heartbeat(
        token,
        {"capabilities": caps, "load": 0.0, "queue_depth": 0, "status": "idle"},
    )
    assert (
        _node_status(node_id) == "idle"
    ), "toggle-off must revert the node to idle (registry transition busy→idle)"

    claim_body = _claim(token, "probe.echo")
    assert claim_body["claimed"] is True, "claims flow again after the toggle-off"
    stage = claim_body["stage"]
    assert stage["capability"] == "probe.echo"


# ---------------------------------------------------------------------------
# 2) 2-Node neighbor E2E: slots = service side of the nodes
# ---------------------------------------------------------------------------


def test_two_node_neighbor_slot_submit_routes_to_target_capability():
    admin_token = _admin()

    submitter_id, submitter_token = _register_and_approve(
        admin_token, "neighbor-submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    target_id, target_token = _register_and_approve(
        admin_token, "neighbor-target", [{"name": "target.cap", "version": "1.0.0"}]
    )

    # The neighbor's node.yaml declaration rides ITS heartbeat (typed):
    # slot "1" = forward (routable), slot "2" = text value slot (not).
    _heartbeat(
        submitter_token,
        {
            "capabilities": [{"name": "submit.misc", "version": "1.0.0"}],
            "forward_slots": [
                {"slot": "1", "type": "forward", "label": "Routable"},
                {"slot": "2", "type": "text", "value": "not-routable"},
            ],
        },
    )
    # the declaration landed
    listed = client.get(
        f"/relay/v2/forward-mappings/slots?node={submitter_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert listed.status_code == 200
    types = {s["slot"]: s["slot_type"] for s in listed.json()["slots"]}
    assert types == {"1": "forward", "2": "text"}, (
        "the neighbor's own typed declaration must be stored relay-side"
    )

    # admin maps the ROUTABLE slot to the target capability
    r = _put_mapping(admin_token, submitter_id, "1", "target.cap")
    assert r.status_code == 200, r.text

    # C5 gate FIRST: submitting through the VALUE slot is refused BEFORE
    # mapping resolution (422 forward_slot_not_routable) — value slots are
    # not routable by declaration, regardless of any mapping.
    r = client.post(
        BASE + "/task-simple",
        json={"capability": "forward", "payload": {"forward_slot": "2"}},
        headers={"Authorization": f"Bearer {submitter_token}"},
    )
    assert r.status_code == 422, r.text
    assert "forward_slot_not_routable" in r.json()["detail"]

    # the forward slot resolves via the mapping and runs AT THE TARGET
    r = client.post(
        BASE + "/task-simple",
        json={"capability": "forward", "payload": {"forward_slot": "1"}},
        headers={"Authorization": f"Bearer {submitter_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["capability"] == "target.cap", "the response echoes the target"
    task_id = body["task_id"]
    row = _stage_row(task_id)
    assert row["capability"] == "target.cap", "stage rewritten to target_capability"
    assert row["forward_slot"] == "1", "slot provenance stays on the stage"

    # the TARGET node claims and runs it — the owner flips to the neighbor
    claim = _claim(target_token, "target.cap")
    assert claim["claimed"] is True
    assert _task_owner(task_id) == target_id, "the task runs on the target node"
