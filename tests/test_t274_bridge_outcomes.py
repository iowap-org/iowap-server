"""T-274 P4/P6: wrapper outcome counters ride the existing heartbeat.

Contract (T-274-Row P4+P6+K3): the a2a wrapper's outcome counters
(delivered / dropped / failed, pending count + oldest pending age) reach
the relay as a ``bridge_outcomes`` field on the EXISTING worker-heartbeat
payload — no new endpoint, no new channel (T-053/T-056 field convention).
The relay persists them on the ``nodes`` row and exposes them in the
node-status API (``GET /relay/v2/discovery/nodes``) and on the dashboard
overview node payload the node cards render.

K3 principle (documentation target, enforced by shape here): only
COUNTERS are pushed in the heartbeat; the raw delivery ledger stays on
the node and is pulled via correlation ids (sent.log ``reply_to``/
``thread_id``). The field therefore carries aggregated numbers per
bridge, never message bodies.

Fixture pattern follows tests/test_owner_node_404.py (fresh_db autouse,
register/approve chains, TestClient).
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.api.v2.auth import limiter as auth_limiter
from relay_server.config import settings
from relay_server.core.auth import generate_secret, hash_secret
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.core.session import sign_user_cookie
from relay_server.main import app

DISCOVERY = "/relay/v2/discovery"
DASHBOARD = "/relay/v2/dashboard"

BRIDGE_OUTCOMES = {
    "a2a": {
        "delivered": 12,
        "dropped": 2,
        "failed": 1,
        "pending_count": 3,
        "oldest_pending_age_seconds": 3661.5,
        "updated_at": "2026-11-18T10:00:00+00:00",
    }
}


@pytest.fixture(autouse=True)
def fresh_db():
    """Temporary database per test with pinned heartbeat windows."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t274_outcomes.db"
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
        settings.session_cookie_secure = False
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
            "INSERT INTO admin_seeds (seed_id, seed_hash, role, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("master", hash_secret(secret), "admin", "2026-01-01T00:00:00+00:00"),
        )
    )
    conn.commit()
    conn.close()
    return secret


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
    assert r.status_code == 200, r.text
    body = r.json()
    return body["node_id"], body["token"]


def _register_admin(secret: str) -> tuple[str, str]:
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "Admin T274 Outcomes",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["node_id"], body["token"]


def _approve(admin_token: str, worker_id: str, caps: list) -> str:
    """Approve the worker and return its NEW runtime token (the token the
    worker got at registration is superseded by approval — see
    test_owner_node_404.py for the reference flow)."""
    r = client.post(
        f"/relay/v2/admin/nodes/{worker_id}/approve",
        json={"role": "service", "capabilities": caps},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _heartbeat(token: str, extra: dict | None = None) -> dict:
    body = {
        "available": True,
        "load": 10.0,
        "load_source": "loadavg",
        "queue_depth": 0,
        "capabilities": [{"name": "outcomes.test.ai", "version": "1.0.0"}],
    }
    if extra:
        body.update(extra)
    return client.post(
        f"{DISCOVERY}/worker-heartbeat",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


# ── tests ─────────────────────────────────────────────────────────────────


def test_worker_heartbeat_accepts_bridge_outcomes():
    """The field rides /worker-heartbeat; the relay answers 200 (no 422)."""
    worker_id, _register_token = _register_worker("outcomes-node", [{"name": "outcomes.test.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "outcomes.test.ai"}])
    r = client.post(
        f"{DISCOVERY}/worker-heartbeat",
        json={
            "available": True,
            "load": 10.0,
            "queue_depth": 0,
            "capabilities": [{"name": "outcomes.test.ai", "version": "1.0.0"}],
            "bridge_outcomes": BRIDGE_OUTCOMES,
        },
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert r.status_code == 200, r.text


def test_node_status_api_exposes_bridge_outcomes():
    """GET /discovery/nodes carries the stored counters on the node row."""
    worker_id, _register_token = _register_worker("outcomes-node", [{"name": "outcomes.test.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "outcomes.test.ai"}])
    hb = _heartbeat(worker_token, {"bridge_outcomes": BRIDGE_OUTCOMES})
    assert hb.status_code == 200, hb.text

    r = client.get(
        f"{DISCOVERY}/nodes",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert r.status_code == 200, r.text
    nodes = r.json()["nodes"]
    row = next(n for n in nodes if n["node_id"] == worker_id)
    assert row["bridge_outcomes"] == BRIDGE_OUTCOMES
    a2a = row["bridge_outcomes"]["a2a"]
    # K3: counters only — the wire shape has no message data at all.
    assert set(a2a) <= {
        "delivered", "dropped", "failed", "pending_count",
        "oldest_pending_age_seconds", "updated_at", "bridge",
    }


def test_heartbeat_without_bridge_outcomes_stays_null():
    """No field in the payload → the column stays NULL (legacy nodes)."""
    worker_id, _register_token = _register_worker("outcomes-node", [{"name": "outcomes.test.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "outcomes.test.ai"}])
    hb = _heartbeat(worker_token)
    assert hb.status_code == 200, hb.text

    r = client.get(
        f"{DISCOVERY}/nodes",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    row = next(n for n in r.json()["nodes"] if n["node_id"] == worker_id)
    assert row["bridge_outcomes"] is None


def test_dashboard_overview_exposes_bridge_outcomes():
    """/api/overview node payload (the node-card source) carries the counters."""
    worker_id, _register_token = _register_worker("outcomes-node", [{"name": "outcomes.test.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "outcomes.test.ai"}])
    hb = _heartbeat(worker_token, {"bridge_outcomes": BRIDGE_OUTCOMES})
    assert hb.status_code == 200, hb.text

    dash = TestClient(app, base_url="https://testserver")
    dash.cookies.set(
        "relay_user",
        sign_user_cookie({"user_id": "__master__", "username": "master"}),
        domain="testserver.local",
    )
    r = dash.get(f"{DASHBOARD}/api/overview")
    assert r.status_code == 200, r.text
    nodes = r.json()["nodes"]
    row = next(n for n in nodes if n["node_id"] == worker_id)
    assert row["bridge_outcomes"] == BRIDGE_OUTCOMES


def test_bridge_outcomes_survive_subsequent_heartbeat_without_field():
    """A heartbeat WITHOUT the field must not wipe the last known counters."""
    worker_id, _register_token = _register_worker("outcomes-node", [{"name": "outcomes.test.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "outcomes.test.ai"}])
    hb = _heartbeat(worker_token, {"bridge_outcomes": BRIDGE_OUTCOMES})
    assert hb.status_code == 200
    hb2 = _heartbeat(worker_token)
    assert hb2.status_code == 200

    r = client.get(
        f"{DISCOVERY}/nodes",
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    row = next(n for n in r.json()["nodes"] if n["node_id"] == worker_id)
    assert row["bridge_outcomes"] == BRIDGE_OUTCOMES
