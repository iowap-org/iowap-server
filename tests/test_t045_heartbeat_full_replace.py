"""T-045 (relay side): worker-heartbeat Full-Replace — unpublish = relay-stop.

Verifizierungstest WITHOUT a code change (the T-045 brief expects the
relay to already behave correctly — the earlier plan to add heartbeat
diff logic was retracted after the code reading):

* the worker-heartbeat endpoint hardcodes ``replace_capabilities=True``
  (api/v2/discovery.py — Ronny 25.11: „die ganze Liste wird übertragen,
  wegfallende fallen im Server weg"),
* ``sync_node_capabilities`` (core/db.py) implements the replace as
  DELETE all node rows + INSERT the posted list,

so when a node pulls a capability back (the node drops it from the
recomposed active profile → the next heartbeat carries it no longer),
the relay's ``node_capabilities`` rows lose it and every discovery /
available view stops offering it. Chain with the node-side T-045 tests:
``capability_publish(published=False)`` → allowlist remove →
``recompile_active_profile`` recompose → heartbeat without the cap →
THIS test's row deletion at the relay.
"""

import json
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
from relay_server.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test; reset limiter and event bus."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        settings.db_path = db_path
        settings.heartbeat_interval_seconds = 1
        settings.heartbeat_timeout_multiplier = 1
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
            "node_name": "Admin T045",
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


def _heartbeat(token: str, capabilities: list, node_id: str = "") -> None:
    r = client.post(
        "/relay/v2/discovery/worker-heartbeat",
        json={
            "node_id": node_id,
            "load": 0.0,
            "queue_depth": 0,
            "available": True,
            "capabilities": capabilities,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text


def _node_cap_rows(node_id: str) -> list[str]:
    conn = get_conn()
    rows = [
        r["capability_name"]
        for r in conn.execute(
            q(
                "SELECT capability_name FROM node_capabilities WHERE node_id = ?",
                (node_id,),
            )
        ).fetchall()
    ]
    conn.close()
    return sorted(rows)


def _discovery_names(admin_token: str, available_only: bool = True) -> set[str]:
    r = client.get(
        f"/relay/v2/discovery/capabilities?available={str(available_only).lower()}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    caps = r.json().get("capabilities", [])
    return {c["name"] for c in caps if c.get("nodes")}


def test_worker_heartbeat_full_replace_drops_unpublished():
    admin_token = _register_admin(_seed_admin())
    node_id, _reg_token = _register_worker(
        "t045-replace-node", [{"name": "cap.a", "version": "1.0.0"}]
    )
    token = _approve(admin_token, node_id, [{"name": "cap.a", "version": "1.0.0"}])

    # heartbeat 1: the node SERVES cap.a + cap.b (published set) — both land
    _heartbeat(token, [{"name": "cap.a"}, {"name": "cap.b"}], node_id=node_id)
    assert _node_cap_rows(node_id) == ["cap.a", "cap.b"]
    assert {"cap.a", "cap.b"} <= _discovery_names(admin_token)

    # heartbeat 2: the node pulled cap.b back (active profile recomposed
    # WITHOUT it) — the Full-Replace drops the relay row
    _heartbeat(token, [{"name": "cap.a"}], node_id=node_id)
    assert _node_cap_rows(node_id) == ["cap.a"]
    names_all = _discovery_names(admin_token, available_only=False)
    names_avail = _discovery_names(admin_token, available_only=True)
    assert "cap.b" not in names_all  # gone from discovery entirely
    assert "cap.a" in names_avail    # the published cap stays offered

    # the discovery query view agrees (no B provider anywhere)
    r = client.get(
        "/relay/v2/discovery/query",
        params={"capability": "cap.b"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    assert r.json()["nodes"] == []
