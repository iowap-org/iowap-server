"""Unknown owner_node_id -> clean 404, not 500 (Parked-Row v5 Punkt 5).

Befund (Drill #2, live belegt 2026-11-16): ``POST /relay/v2/scheduler/tasks``
und ``POST /relay/v2/scheduler/task-simple`` validated die Capability, aber
nicht die Existenz von ``body.owner_node_id``. ``Scheduler.create_task``
inserts blindly; ``tasks.owner_node_id`` carries an FK to ``nodes(node_id)``
(core/db.py, ``PRAGMA foreign_keys=ON``), so an unknown owner surfaced as an
unhandled ``sqlite3.IntegrityError`` — an HTTP 500 with no detail body that
wrappers cannot distinguish from a relay fault (blind retry or silent drop).

Soll: BEFORE ``Scheduler.create_task``, both endpoints reject a set
``owner_node_id`` that is unknown to the ``nodes`` table with
``404 {"detail": "Owner node '<node_id>' not found"}`` — 404 (the id is
format-valid, the object is missing), never 422, never 500.
``owner_node_id=None`` (ownerless fan-out) and unknown-free known ids keep
the previous behavior untouched (plan Punkt 2: no schema change, no
migration, no behavior change for known owners).

Fixture pattern follows tests/test_t229_priority_claim.py (fresh_db autouse
fixture with a temp database and pinned heartbeat settings, TestClient,
register/approve chains). The capability holder keeps the submitted
capability real in the registry so ``/task-simple``'s capability check
(api/v2/scheduler.py) passes and the tests isolate the owner dimension.
The unknown owner id "NOVAFORGE" is the label observed live in the 2026-11-16
drill: 9 characters, format-valid ([A-Z0-9]{8,14}), no node row behind it.
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
from relay_server.main import app

BASE = "/relay/v2/scheduler"
CAP = "owner404.cap"
UNKNOWN_OWNER = "NOVAFORGE"


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test and reset the event-bus history."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "owner404.db"
        # Pin the defaults (see test_t229 fixture comment): shrinking
        # heartbeat windows make multi-step register/approve chains flaky.
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
        auth_limiter.reset()
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


# (event_bus imported above with the other relay_server names)

client = TestClient(app)


def _seed_admin() -> str:
    # register-admin accepts ONLY seed_id "master" (core/auth.py:277)
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


def _register_admin(secret: str) -> tuple[str, str]:
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "Admin Owner404",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200, r.text
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
    assert r.status_code == 200, r.text
    body = r.json()
    return body["node_id"], body["token"]


def _approve(admin_token: str, worker_id: str, caps: list) -> str:
    r = client.post(
        f"/relay/v2/admin/nodes/{worker_id}/approve",
        json={"role": "service", "capabilities": caps},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _world() -> dict:
    """Approved submitter + approved capability holder (real capability)."""
    _admin_id, admin_token = _register_admin(_seed_admin())
    submitter_id, _pending = _register_worker(
        "owner404-submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    submitter_token = _approve(
        admin_token, submitter_id, [{"name": "submit.misc", "version": "1.0.0"}]
    )
    holder_id, _pending = _register_worker(
        "owner404-holder", [{"name": CAP, "version": "1.0.0"}]
    )
    _approve(admin_token, holder_id, [{"name": CAP, "version": "1.0.0"}])
    return {"submitter": submitter_id, "token": submitter_token, "holder": holder_id}


def _post_tasks(token: str, owner: str | None) -> "object":
    return client.post(
        BASE + "/tasks",
        json={
            "task_name": "owner404",
            "stages": [
                {
                    "stage_name": "main",
                    "capability": CAP,
                    "depends_on": [],
                    "payload": {"k": "v"},
                }
            ],
            "owner_node_id": owner,
        },
        headers={"Authorization": f"Bearer {token}"},
    )


def _post_task_simple(token: str, owner: str | None) -> "object":
    return client.post(
        BASE + "/task-simple",
        json={"capability": CAP, "payload": {"k": "v"}, "owner_node_id": owner},
        headers={"Authorization": f"Bearer {token}"},
    )


def _task_count() -> int:
    conn = get_conn()
    try:
        row = conn.execute(q("SELECT COUNT(*) AS c FROM tasks")).fetchone()
        return row["c"] if row is not None else 0
    finally:
        conn.close()


def _owner_of(task_id: str):
    conn = get_conn()
    try:
        row = conn.execute(
            q("SELECT owner_node_id FROM tasks WHERE task_id = ?", (task_id,))
        ).fetchone()
        return row["owner_node_id"] if row is not None else None
    finally:
        conn.close()


# ── unknown owner -> 404 with the id in the detail ───────────────


def test_create_task_unknown_owner_returns_404():
    w = _world()
    r = _post_tasks(w["token"], owner=UNKNOWN_OWNER)
    assert r.status_code == 404, f"was: {r.status_code}: {r.text}"
    assert UNKNOWN_OWNER in r.json()["detail"], r.json()
    # Validation happens BEFORE create_task: no task row may leak.
    assert _task_count() == 0, _task_count()


def test_task_simple_unknown_owner_returns_404():
    w = _world()
    r = _post_task_simple(w["token"], owner=UNKNOWN_OWNER)
    assert r.status_code == 404, f"was: {r.status_code}: {r.text}"
    assert UNKNOWN_OWNER in r.json()["detail"], r.json()
    assert _task_count() == 0, _task_count()


# ── known owner / ownerless: behavior unchanged (2xx) ────────────


def test_create_task_known_owner_returns_2xx():
    w = _world()
    r = _post_tasks(w["token"], owner=w["holder"])
    assert r.status_code == 200, f"was: {r.status_code}: {r.text}"
    assert _owner_of(r.json()["task"]["task_id"]) == w["holder"]


def test_create_task_ownerless_returns_2xx():
    w = _world()
    r = _post_tasks(w["token"], owner=None)
    assert r.status_code == 200, f"was: {r.status_code}: {r.text}"
    assert _owner_of(r.json()["task"]["task_id"]) is None


def test_task_simple_known_owner_returns_2xx():
    w = _world()
    r = _post_task_simple(w["token"], owner=w["holder"])
    assert r.status_code == 200, f"was: {r.status_code}: {r.text}"
    assert _owner_of(r.json()["task_id"]) == w["holder"]


def test_task_simple_ownerless_returns_2xx():
    w = _world()
    r = _post_task_simple(w["token"], owner=None)
    assert r.status_code == 200, f"was: {r.status_code}: {r.text}"
    assert _owner_of(r.json()["task_id"]) is None
