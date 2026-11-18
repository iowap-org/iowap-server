"""T-274 P7: schema-violation warn path on submit (no hard 422).

Contract (T-274-Row P7 + Grimbart refinement): the node heartbeats
``schema_hash`` (SHA-256 over the canonical input_schema JSON) next to
``input_schema``. At submit time the relay checks the stage payload
against the input_schema stored from the LAST heartbeat of that
capability:

* violation  -> the submit succeeds (task runs normally) but the
  response / task view carries a ``schema_violation`` warn field
  (state ``schema_violation``) listing the violations.
* no fresh basis -> the capability was heartbeated without
  ``input_schema`` or without ``schema_hash`` (e.g. schemata changed on
  the node without a heartbeat snapshot the relay can vouch for), or a
  multi-stage /tasks stage references an unknown capability: state
  ``unvalidated`` — visibly marked instead of silently warned-false.
* validated  -> no warn field at all (absence = validated).

The warn path never fails the submit and never mutates the payload —
the relay matches and routes only (class rule).
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

SCHED = "/relay/v2/scheduler"
DISCOVERY = "/relay/v2/discovery"

SCHEMA_HASH = "b" * 64
INPUT_SCHEMA = {
    "fields": {
        "prompt": {
            "name": "prompt",
            "type": "string",
            "required": True,
            "description": "The main instruction. Example: 'Summarize this text.'",
        },
        "max_tokens": {
            "name": "max_tokens",
            "type": "integer",
            "required": False,
            "description": "Token ceiling. Example: 512",
        },
    }
}


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t274_schema.db"
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
            "node_name": "Admin T274 Schema",
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


def _publish(worker_token: str, caps: list[dict]) -> None:
    """Heartbeat the full capability snapshot (replace mode)."""
    r = client.post(
        f"{DISCOVERY}/worker-heartbeat",
        json={
            "available": True,
            "load": 5.0,
            "queue_depth": 0,
            "capabilities": caps,
        },
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert r.status_code == 200, r.text


def _schema_cap(name: str = "chat.t274.ai") -> dict:
    return {
        "name": name,
        "version": "1.0.0",
        "available": True,
        "input_schema": INPUT_SCHEMA,
        "schema_hash": SCHEMA_HASH,
    }


def _submit_simple(worker_token: str, payload: dict, capability: str = "chat.t274.ai") -> dict:
    r = client.post(
        f"{SCHED}/task-simple",
        json={"capability": capability, "payload": payload, "name": "t274"},
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    return r


# ── tests ─────────────────────────────────────────────────────────────────


def test_task_simple_violation_warns_but_task_runs():
    """Required field missing -> 200 + schema_violation warn, stage pending."""
    worker_id, _register_token = _register_worker("schema-node", [{"name": "chat.t274.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "chat.t274.ai"}])
    _publish(worker_token, [_schema_cap()])

    r = _submit_simple(worker_token, {"max_tokens": 512})
    assert r.status_code == 200, r.text
    body = r.json()
    warn = body["schema_violation"]
    assert warn["state"] == "schema_violation"
    assert warn["capability"] == "chat.t274.ai"
    assert warn["schema_hash"] == SCHEMA_HASH
    fields = {v["field"] for v in warn["violations"]}
    assert fields == {"prompt"}

    # The task continues normally: pending, warn persisted on the stage.
    view = client.get(
        f"{SCHED}/tasks/{body['task_id']}",
        headers={"Authorization": f"Bearer {worker_token}"},
    ).json()
    stage = view["stages"][0]
    assert stage["status"] == "pending"
    assert stage["schema_violation"]["state"] == "schema_violation"


def test_task_simple_valid_payload_carries_no_warn():
    """A conforming payload validates silently — no warn field value."""
    worker_id, _register_token = _register_worker("schema-node", [{"name": "chat.t274.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "chat.t274.ai"}])
    _publish(worker_token, [_schema_cap()])

    r = _submit_simple(worker_token, {"prompt": "hello"})
    assert r.status_code == 200, r.text
    assert r.json()["schema_violation"] is None

    view = client.get(
        f"{SCHED}/tasks/{r.json()['task_id']}",
        headers={"Authorization": f"Bearer {worker_token}"},
    ).json()
    assert view["stages"][0]["schema_violation"] is None


def test_task_simple_type_mismatch_warns():
    """max_tokens is integer in the schema — a string payload warns."""
    worker_id, _register_token = _register_worker("schema-node", [{"name": "chat.t274.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "chat.t274.ai"}])
    _publish(worker_token, [_schema_cap()])

    r = _submit_simple(worker_token, {"prompt": "hi", "max_tokens": "waytoo many"})
    assert r.status_code == 200, r.text
    warn = r.json()["schema_violation"]
    assert any(v["field"] == "max_tokens" and v["issue"] == "type" for v in warn["violations"])


def test_unvalidated_when_capability_has_no_schema_basis():
    """Heartbeated WITHOUT input_schema/schema_hash -> unvalidated, not 422."""
    worker_id, _register_token = _register_worker("schema-node", [{"name": "chat.bare.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "chat.bare.ai"}])
    # Pre-T-274-style publish: capability present, no schema metadata.
    _publish(worker_token, [{"name": "chat.bare.ai", "version": "1.0.0"}])

    r = _submit_simple(worker_token, {"prompt": "hi"}, capability="chat.bare.ai")
    assert r.status_code == 200, r.text
    warn = r.json()["schema_violation"]
    assert warn["state"] == "unvalidated"
    assert warn["reason"] == "no_schema_basis"


def test_unvalidated_when_input_schema_sent_without_hash():
    """input_schema WITHOUT schema_hash cannot be vouched for -> unvalidated.

    The hash is the freshness signal: only a heartbeat that carried the
    hash of the schema it shipped gives the relay a snapshot worth
    validating against. A schema alone (mixed-version heartbeat) falls
    back to unvalidated instead of warning against a stale basis.
    """
    worker_id, _register_token = _register_worker("schema-node", [{"name": "chat.hashed.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "chat.hashed.ai"}])
    _publish(worker_token, [
        {"name": "chat.hashed.ai", "version": "1.0.0",
         "input_schema": INPUT_SCHEMA, "schema_hash": None},
    ])

    r = _submit_simple(worker_token, {"prompt": "hi"}, capability="chat.hashed.ai")
    assert r.status_code == 200, r.text
    warn = r.json()["schema_violation"]
    assert warn["state"] == "unvalidated"
    assert warn["reason"] == "no_schema_basis"


def test_multistage_tasks_warns_only_on_violating_stage():
    """/tasks: warns ride the affected stage rows; valid stages stay clean."""
    worker_id, _register_token = _register_worker("schema-node", [{"name": "chat.t274.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "chat.t274.ai"}])
    _publish(worker_token, [_schema_cap()])

    r = client.post(
        f"{SCHED}/tasks",
        json={
            "task_name": "t274-multi",
            "owner_node_id": worker_id,
            "metadata": {"board_task_id": "T-274"},
            "stages": [
                {"stage_name": "good", "capability": "chat.t274.ai", "payload": {"prompt": "hi"}},
                {"stage_name": "bad", "capability": "chat.t274.ai", "payload": {}},
            ],
        },
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["task"]["board_task_id"] == "T-274"
    warns = {s["stage_name"]: s["schema_violation"] for s in view["stages"]}
    assert warns["good"] is None
    assert warns["bad"]["state"] == "schema_violation"


def test_multistage_unknown_capability_marks_unvalidated():
    """A /tasks stage referencing an unknown capability runs (queued) but
    its schema state is honestly ``unvalidated`` (capability_unknown)."""
    worker_id, _register_token = _register_worker("schema-node", [{"name": "chat.t274.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "chat.t274.ai"}])
    _publish(worker_token, [_schema_cap()])

    r = client.post(
        f"{SCHED}/tasks",
        json={
            "task_name": "t274-unknown",
            "stages": [
                {"stage_name": "ghost", "capability": "ghost.t274.ai", "payload": {"x": 1}},
            ],
        },
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert r.status_code == 200, r.text
    stage = r.json()["stages"][0]
    assert stage["status"] == "pending"
    assert stage["schema_violation"]["state"] == "unvalidated"
    assert stage["schema_violation"]["reason"] == "capability_unknown"


def test_schema_basis_follows_last_heartbeat():
    """The LAST heartbeat defines the vouchable basis: a heartbeat that
    replaces the capability WITHOUT input_schema/schema_hash drops the
    basis — a later submit is ``unvalidated``, not warned against the
    stale schema (Fehlverhalten sichtbar statt still konsistent)."""
    worker_id, _register_token = _register_worker("schema-node", [{"name": "chat.t274.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "chat.t274.ai"}])
    _publish(worker_token, [_schema_cap()])
    # The node ships a schema-less update (mixed-version heartbeat).
    _publish(worker_token, [{"name": "chat.t274.ai", "version": "2.0.0"}])

    r = _submit_simple(worker_token, {"prompt": "hi"})
    assert r.status_code == 200, r.text
    warn = r.json()["schema_violation"]
    assert warn is not None
    assert warn["state"] == "unvalidated"
    assert warn["reason"] == "no_schema_basis"


def test_heartbeat_without_bridge_outcomes_does_not_wipe_board_link_and_warn():
    """P4/P6/P7/P8 interaction smoke: a second heartbeat WITHOUT the
    outcomes field keeps the last counters, and the persisted warn on an
    earlier submitted stage stays readable."""

    worker_id, _register_token = _register_worker("schema-node", [{"name": "chat.t274.ai"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "chat.t274.ai"}])
    _publish(worker_token, [_schema_cap()])
    submitted = _submit_simple(worker_token, {})
    assert submitted.status_code == 200
    warn = submitted.json()["schema_violation"]
    assert warn["state"] == "schema_violation"

    # A heartbeat without bridge_outcomes must not clear counters that a
    # previous heartbeat with the field has set (P4/P6 survival rule).
    OUTCOMES = {"a2a": {"delivered": 5, "dropped": 0, "failed": 0,
                        "pending_count": 1, "oldest_pending_age_seconds": 12.5}}
    hb = client.post(
        f"{DISCOVERY}/worker-heartbeat",
        json={
            "available": True,
            "load": 5.0,
            "queue_depth": 0,
            "capabilities": [_schema_cap()],
            "bridge_outcomes": OUTCOMES,
        },
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert hb.status_code == 200, hb.text
    hb2 = client.post(
        f"{DISCOVERY}/worker-heartbeat",
        json={
            "available": True,
            "load": 5.0,
            "queue_depth": 0,
            "capabilities": [_schema_cap()],
        },
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert hb2.status_code == 200, hb2.text

    nodes = client.get(
        f"{DISCOVERY}/nodes",
        headers={"Authorization": f"Bearer {worker_token}"},
    ).json()["nodes"]
    row = next(n for n in nodes if n["node_id"] == worker_id)
    assert row["bridge_outcomes"] == OUTCOMES

    # The warn persisted at submit survived on the stage (read via view).
    view = client.get(
        f"{SCHED}/tasks/{submitted.json()['task_id']}",
        headers={"Authorization": f"Bearer {worker_token}"},
    ).json()
    assert view["stages"][0]["schema_violation"]["state"] == "schema_violation"
