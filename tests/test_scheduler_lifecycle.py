"""T-189: Scheduler lifecycle repair — acceptance tests (RED state).

Covers the six audit findings: busy-node completion (F-02/F-03),
unconditional claim gate (F-04), linear failure policy (F-05), orphaned
provider query (F-06), and status registry gaps (F-08). Fixture pattern
follows tests/test_discovery_visibility.py (fresh_db autouse fixture,
TestClient, RELAY_DB_PATH="" env reset). Node status is set via direct
DB update inside the test (T-080 pattern): busy is a server-set status
and nodes may not request it via the API.
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
from relay_server.core.status import (
    STAGE_STATUSES,
    node_can_transition,
    stage_can_transition,
    stage_statuses_in_category,
    task_statuses_in_category,
)
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




def _set_node_status(node_id: str, new_status: str) -> None:
    conn = get_conn()
    conn.execute(
        q("UPDATE nodes SET status = ? WHERE node_id = ?", (new_status, node_id)),
    )
    conn.commit()
    conn.close()


def _stage_status(stage_id: str) -> str:
    conn = get_conn()
    row = conn.execute(
        q("SELECT status FROM task_stages WHERE stage_id = ?", (stage_id,)),
    ).fetchone()
    conn.close()
    return row["status"]


def _create_task(admin_token: str, stages: list) -> dict:
    r = client.post(
        "/relay/v2/scheduler/tasks",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"task_name": "T189 task", "stages": stages},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _claim(runtime: str, body: dict | None = None):
    return client.post(
        "/relay/v2/scheduler/claim",
        headers={"Authorization": f"Bearer {runtime}"},
        json=body or {},
    )


def _complete(runtime: str, stage_id: str, result: dict | None = None):
    return client.post(
        f"/relay/v2/scheduler/stages/{stage_id}/complete",
        headers={"Authorization": f"Bearer {runtime}"},
        json={"result": result or {"ok": True}},
    )


def _note(runtime: str, task_id: str, message: str, kind: str = "longrun"):
    return client.post(
        f"/relay/v2/scheduler/tasks/{task_id}/notes",
        headers={"Authorization": f"Bearer {runtime}"},
        json={"message": message, "kind": kind},
    )


def _task_status_changed(new_status: str) -> list:
    return [
        e
        for e in event_bus.recent(200)
        if e.get("type") == "status_changed"
        and e.get("payload", {}).get("entity_type") == "task"
        and e.get("payload", {}).get("new_status") == new_status
    ]


def _stage_of(task_view: dict, stage_name: str) -> dict:
    for s in task_view["stages"]:
        if s["stage_name"] == stage_name:
            return s
    raise AssertionError(f"stage {stage_name!r} not found")


# ── Test 1 (F-04): claim gate applies with explicit capability ───────────


def test_claim_gate_applies_to_explicit_capability():
    """F-04: busy node cannot claim even with an explicit capability; a
    node without the requested capability gets no stage either."""
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)
    worker_id, runtime = _register_worker(
        "w-gate", [{"name": "build", "version": "1.0.0"}]
    )
    runtime = _approve(
        admin_token, worker_id, [{"name": "build", "version": "1.0.0"}]
    )
    _hb(runtime)
    _create_task(admin_token, [{"stage_name": "s1", "capability": "build"}])

    # Busy node, explicit matching capability → claim must be refused by
    # the core gate with a 200 response (claim uses the alive dependency,
    # so eligibility stays the scheduler's decision — plan Task 2).
    _set_node_status(worker_id, "busy")
    r = _claim(runtime, {"capability": "build"})
    assert r.status_code == 200, (
        f"claim endpoint rejected a live busy node: {r.status_code} {r.text}"
    )
    assert r.json()["claimed"] is False, (
        "F-04: busy node claimed a stage via explicit capability"
    )
    r = _claim(runtime)
    assert r.status_code == 200, r.text
    assert r.json()["claimed"] is False

    # Back online: explicit capability still must not hand out stages the
    # node does not advertise.
    _set_node_status(worker_id, "online")
    body = _claim(runtime, {"capability": "not-advertised"}).json()
    assert body["claimed"] is False, (
        "F-04: node claimed a capability it does not advertise"
    )


# ── Test 2 (F-02): busy node can complete and note ───────────────────────


def test_busy_node_can_complete_and_note():
    """F-02: a busy node completes its claimed stage (200) and posts a
    longrun note (200); the stage flips claimed→accepted (F-08)."""
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)
    worker_id, runtime = _register_worker(
        "w-busy", [{"name": "build", "version": "1.0.0"}]
    )
    runtime = _approve(
        admin_token, worker_id, [{"name": "build", "version": "1.0.0"}]
    )
    _hb(runtime)
    task = _create_task(admin_token, [{"stage_name": "s1", "capability": "build"}])
    claim = _claim(runtime).json()
    assert claim["claimed"] is True
    stage_id = claim["stage"]["stage_id"]
    _set_node_status(worker_id, "busy")

    r = _note(runtime, task["task"]["task_id"], "still working")
    assert r.status_code == 200, f"F-02: longrun note rejected: {r.text}"
    assert _stage_status(stage_id) == "accepted"

    r = _complete(runtime, stage_id)
    assert r.status_code == 200, f"F-02: busy complete rejected: {r.text}"
    assert r.json()["status"] == "completed"


# ── Test 3 (F-03): completion counter includes accepted stages ──────────


def test_completion_counts_accepted_stages():
    """F-03: completing a stage from accepted counts towards task
    completion; the task completes only when it should."""
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)
    worker_id, runtime = _register_worker(
        "w-f03", [{"name": "build", "version": "1.0.0"}]
    )
    runtime = _approve(
        admin_token, worker_id, [{"name": "build", "version": "1.0.0"}]
    )
    _hb(runtime)
    task = _create_task(
        admin_token,
        [
            {"stage_name": "s1", "capability": "build", "depends_on": []},
            {"stage_name": "s2", "capability": "build", "depends_on": []},
        ],
    )
    task_id = task["task"]["task_id"]

    claim1 = _claim(runtime).json()
    assert claim1["claimed"] is True
    sid1 = claim1["stage"]["stage_id"]

    claim2 = _claim(runtime).json()
    assert claim2["claimed"] is True
    sid2 = claim2["stage"]["stage_id"]
    assert sid2 != sid1

    # longrun note flips sid2 claimed → accepted (T-154).
    r = _note(runtime, task_id, "long run")
    assert r.status_code == 200, r.text
    assert _stage_status(sid2) == "accepted"

    # The premature-completion bug: while sid2 is accepted, completing sid1
    # must NOT mark the task completed. Today the counter query
    # (status IN ('pending','claimed')) ignores accepted/orphaned stages
    # and flips the task to completed anyway (F-03).
    r = _complete(runtime, claim1["stage"]["stage_id"])
    assert r.status_code == 200, r.text

    view = client.get(
        f"/relay/v2/scheduler/tasks/{task_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert view["task"]["status"] != "completed", (
        "F-03: task completed prematurely while another stage is accepted"
    )

    # Complete from accepted: must succeed and complete the task.
    r = _complete(runtime, sid2)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"

    view = client.get(
        f"/relay/v2/scheduler/tasks/{task_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert view["task"]["status"] == "completed", (
        "F-03: task did not complete although all stages are terminal"
    )
    assert _task_status_changed("completed"), (
        "task-completed status_changed event missing"
    )


# ── Test 4 (F-05): linear failure policy ────────────────────────────────


def test_linear_failure_policy():
    """F-05: a permanently failed stage fails the whole task while
    downstream stages stay pending (no zombie running task)."""
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)
    worker_id, runtime = _register_worker(
        "w-f05", [{"name": "build", "version": "1.0.0"}]
    )
    runtime = _approve(
        admin_token, worker_id, [{"name": "build", "version": "1.0.0"}]
    )
    _hb(runtime)
    task = _create_task(
        admin_token,
        [
            {"stage_name": "s1", "capability": "build"},
            {"stage_name": "s2", "capability": "build", "depends_on": ["s1"]},
        ],
    )
    task_id = task["task"]["task_id"]
    s1 = _stage_of(task, "s1")
    s2 = _stage_of(task, "s2")

    claim = _claim(runtime).json()
    assert claim["claimed"] is True
    assert claim["stage"]["stage_id"] == s1["stage_id"]

    # Force claim-TTL expiry with the retry budget exhausted.
    conn = get_conn()
    conn.execute(
        q(
            "UPDATE task_stages SET retry_count = ?, claim_expires_at = ? "
            "WHERE stage_id = ?",
            (settings.max_retries, "2020-01-01T00:00:00+00:00", s1["stage_id"]),
        ),
    )
    conn.commit()
    conn.close()

    from relay_server.core.scheduler import Scheduler

    result = Scheduler.release_or_fail_claims()
    assert s1["stage_id"] in result["failed"], "stage did not fail permanently"
    assert result["tasks_failed"] == [task_id], (
        "F-05: task not failed despite a permanently failed stage"
    )
    assert _stage_status(s2["stage_id"]) == "pending", (
        "F-05: downstream stage must stay pending until cancel"
    )
    assert _task_status_changed("failed"), (
        "task-failed status_changed event missing"
    )


def test_linear_failure_policy_fail_orphaned_path():
    """F-05 via fail_orphaned_stages: an orphan-failed stage fails its
    task too (linear policy applies to both sweep paths)."""
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)
    worker_id, runtime = _register_worker(
        "w-f05b", [{"name": "build", "version": "1.0.0"}]
    )
    runtime = _approve(
        admin_token, worker_id, [{"name": "build", "version": "1.0.0"}]
    )
    _hb(runtime)
    # The real F-05 gap in the orphan path: s1's provider goes offline
    # (s1 orphan-fails) while s2 has a live provider and stays pending.
    # _fail_tasks_if_all_stages_done only fails the task when ALL stages
    # are terminal — the linear policy must fail it on s1 alone.
    worker2_id, runtime2 = _register_worker(
        "w-f05b-dep", [{"name": "deploy", "version": "1.0.0"}]
    )
    runtime2 = _approve(
        admin_token, worker2_id, [{"name": "deploy", "version": "1.0.0"}]
    )
    _hb(runtime2)

    task = _create_task(
        admin_token,
        [
            {"stage_name": "s1", "capability": "build"},
            {"stage_name": "s2", "capability": "deploy"},
        ],
    )
    task_id = task["task"]["task_id"]
    _set_node_status(worker_id, "offline")

    from relay_server.core.scheduler import Scheduler

    result = Scheduler.fail_orphaned_stages()
    assert result["stages_failed"], "stage did not fail"
    assert _stage_status(_stage_of(task, "s2")["stage_id"]) == "pending"
    assert result["tasks_failed"] == [task_id], (
        "F-05: task stays alive although a stage failed permanently"
    )


# ── Test 5 (F-06): orphan sweep respects busy providers ──────────────────


def test_orphan_sweep_respects_busy_provider():
    """F-06: a busy provider keeps pending stages alive; an offline
    provider lets them fail."""
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)
    worker_id, runtime = _register_worker(
        "w-f06", [{"name": "build", "version": "1.0.0"}]
    )
    runtime = _approve(
        admin_token, worker_id, [{"name": "build", "version": "1.0.0"}]
    )
    _hb(runtime)
    task = _create_task(admin_token, [{"stage_name": "s1", "capability": "build"}])
    stage_id = task["stages"][0]["stage_id"]
    _set_node_status(worker_id, "busy")

    from relay_server.core.scheduler import Scheduler

    result = Scheduler.fail_orphaned_stages()
    assert result["stages_failed"] == [], (
        "F-06: busy provider's stage failed spuriously"
    )
    assert _stage_status(stage_id) == "pending"

    _set_node_status(worker_id, "offline")
    result = Scheduler.fail_orphaned_stages()
    assert stage_id in result["stages_failed"], (
        "F-06: offline provider's stage did not fail"
    )


# ── Test 6 (F-08): registry covers real transitions ────────────────────


def test_registry_covers_real_transitions():
    """F-08: registry allows claimed→accepted and offline→online; existing
    transition sets stay unchanged."""
    assert stage_can_transition("claimed", "accepted") is True
    assert node_can_transition("offline", "online") is True
    assert node_can_transition("busy", "pending") is False
    assert stage_can_transition("completed", "accepted") is False
    assert "accepted" in STAGE_STATUSES["claimed"].allowed_transitions


# ── Test 7: longrun note recovery via API (integration) ─────────────────


def test_longrun_note_recovery_via_api():
    """F-02+F-03+F-08 combined: claimed → longrun note → accepted →
    complete → task completed, all through the public API."""
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)
    worker_id, runtime = _register_worker(
        "w-e2e", [{"name": "build", "version": "1.0.0"}]
    )
    runtime = _approve(
        admin_token, worker_id, [{"name": "build", "version": "1.0.0"}]
    )
    _hb(runtime)
    task = _create_task(admin_token, [{"stage_name": "s1", "capability": "build"}])
    task_id = task["task"]["task_id"]

    claim = _claim(runtime).json()
    assert claim["claimed"] is True
    stage_id = claim["stage"]["stage_id"]
    _set_node_status(worker_id, "busy")

    r = _note(runtime, task_id, "long run")
    assert r.status_code == 200, r.text
    assert _stage_status(stage_id) == "accepted"

    r = _complete(runtime, stage_id)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"

    view = client.get(
        f"/relay/v2/scheduler/tasks/{task_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert view["task"]["status"] == "completed"
    assert _task_status_changed("completed")


# ── Test 8: category helpers for SQL predicates ─────────────────────────


def test_stage_and_task_category_helpers():
    """F-03/F-05 helpers: category views must match the registry."""
    from relay_server.core.status import StatusCategory

    stage_pending = set(stage_statuses_in_category(StatusCategory.PENDING))
    assert {"pending", "accepted", "orphaned"} <= stage_pending
    assert set(stage_statuses_in_category(StatusCategory.BUSY)) == {"claimed"}
    stage_terminal = set(stage_statuses_in_category(StatusCategory.TERMINAL))
    assert {"completed", "failed", "timed_out", "cancelled"} <= stage_terminal

    task_pending = set(task_statuses_in_category(StatusCategory.PENDING))
    assert {"pending", "accepted", "awaiting_subtasks", "needs_input"} <= task_pending
    task_terminal = set(task_statuses_in_category(StatusCategory.TERMINAL))
    assert {"completed", "failed", "timed_out", "cancelled"} <= task_terminal
