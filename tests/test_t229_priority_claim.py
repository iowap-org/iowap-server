"""T-229: priority-aware claim ordering + task-simple queued (was hard 503).

Before this change, ``tasks.priority`` was stored, indexed and shown in
listings but completely ignored by the claim path — whoever polled first
won, regardless of priority. ``POST /scheduler/task-simple`` additionally
hard-503'd when the matched capability was unavailable (auto-busy,
queue_depth >= 1), so burst submits lost work instead of queueing.

Now: ``claim_stage`` orders candidates ``tasks.priority DESC,
tasks.created_at ASC, stages.sequence ASC`` (FIFO among equal priority),
and task-simple stores the task pending with the TOP priority (10) by
default (explicit ``priority`` still overrides). The T-228 decision
(Ronny 2026-10-15): task-simple is "do it NOW" — queued at the head of
the claim order, ties resolved FIFO.

Fixture pattern follows tests/test_scheduler_lifecycle.py (fresh_db
autouse fixture, TestClient, RELAY_DB_PATH="" env reset). The
T-228-shape scenario mirrors the live incident: a healthy SUBMITTER
node posts task-simple while the capability's serving node is busy
(node status busy + ``nodes.available = 0`` — the availability chain
``eff = nodes.available AND nc.available``, core/discovery.py T-195).
Node state is set via direct DB update (T-080 pattern): busy is a
server-set status, not node-requested.
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
from relay_server.core.scheduler import Scheduler
from relay_server.main import app

BASE = "/relay/v2/scheduler"


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test and reset the event-bus history."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t229.db"
        # Pin the defaults: earlier suites (e.g. test_scheduler_lifecycle)
        # shrink the alive window to 1s and never restore it — that turns
        # multi-step register/approve chains into flaky 404-capability
        # lookups (last_seen > threshold fails).
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
        auth_limiter.reset()
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


client = TestClient(app)


def _seed_admin() -> str:
    # register-admin accepts ONLY seed_id "master" (core/auth.py:277)
    secret = generate_secret("adm_")
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO admin_seeds (seed_id, seed_hash, role, created_at) VALUES (?, ?, ?, ?)",
            (
                "master",
                hash_secret(secret),
                "admin",
                "2026-01-01T00:00:00+00:00",
            ),
        )
    )
    conn.commit()
    conn.close()
    return secret


def _register_admin(secret: str) -> tuple[str, str]:
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "Admin T229",
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


def _worker_with_cap(capability: str) -> tuple[str, str]:
    """Register + approve one worker advertising exactly this capability."""
    _admin_id, admin_token = _register_admin(_seed_admin())
    node_id, _pending_token = _register_worker(
        "worker-" + capability.replace(".", "-"),
        [{"name": capability, "version": "1.0.0"}],
    )
    token = _approve(admin_token, node_id, [{"name": capability, "version": "1.0.0"}])
    return node_id, token


def _two_nodes(capability: str) -> dict:
    """T-228 incident shape: healthy submitter + cap-holding worker.

    Returns worker_node_id, worker_token, submitter_token, admin_node_id.
    The submitter stays available (in the incident the consumer was a
    separate node posting against a busy provider).
    """
    _admin_id, admin_token = _register_admin(_seed_admin())
    worker_id, _pending = _register_worker(
        "worker-" + capability.replace(".", "-"),
        [{"name": capability, "version": "1.0.0"}],
    )
    worker_token = _approve(
        admin_token, worker_id, [{"name": capability, "version": "1.0.0"}]
    )
    submitter_id, _pending = _register_worker(
        "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    submitter_token = _approve(
        admin_token, submitter_id, [{"name": "submit.misc", "version": "1.0.0"}]
    )
    return {
        "worker": worker_id,
        "worker_token": worker_token,
        "submitter": submitter_id,
        "submitter_token": submitter_token,
        "admin_node_id": _admin_id,
    }


def _set_worker_busy(node_id: str, busy: bool) -> None:
    """T-080 pattern: busy is server-set; unavailable = busy + available=0."""
    conn = get_conn()
    conn.execute(
        q(
            "UPDATE nodes SET status = ?, available = ? WHERE node_id = ?",
            ("busy" if busy else "approved", 0 if busy else 1, node_id),
        )
    )
    conn.commit()
    conn.close()


def _task_priority(task_id: str):
    conn = get_conn()
    row = conn.execute(
        q("SELECT priority FROM tasks WHERE task_id = ?", (task_id,))
    ).fetchone()
    conn.close()
    return None if row is None else row["priority"]


def _new_task(name: str, capability: str, priority: int = 0, owner: str | None = None) -> str:
    result = Scheduler.create_task(
        task_name=name,
        stages=[
            {
                "stage_name": "main",
                "capability": capability,
                "depends_on": None,
                "timeout_seconds": None,
                "payload": {"k": name},
            }
        ],
        priority=priority,
        owner_node_id=owner,
        submitted_by="test-suite",
    )
    return result["task_id"]


# ── Claim ordering (core) ───────────────────────────────────────


class TestClaimPriorityOrder:
    def test_claim_respects_priority_over_creation_order(self):
        node_id, _ = _worker_with_cap("capx")
        low = _new_task("low", "capx", priority=0)
        mid = _new_task("mid", "capx", priority=5)
        high = _new_task("high", "capx", priority=10)

        claimed = [
            Scheduler.claim_stage(node_id, capability="capx")["task_id"] for _ in range(3)
        ]
        assert claimed == [high, mid, low], (
            "claim order must be priority DESC (creation order is low/mid/high)"
        )

    def test_claim_fifo_within_same_priority(self):
        node_id, _ = _worker_with_cap("capx")
        first = _new_task("first", "capx", priority=10)
        second = _new_task("second", "capx", priority=10)

        claimed = [
            Scheduler.claim_stage(node_id, capability="capx")["task_id"] for _ in range(2)
        ]
        assert claimed == [first, second], (
            "equal priority (Ronny: mehrere hoch priorisierte Tasks -> FIFO)"
            " resolves by creation order"
        )

    def test_owner_restricted_high_prio_does_not_block_rest(self):
        nodes = _two_nodes("capx")
        worker_id = nodes["worker"]
        owned_by_other = _new_task(
            "pinned", "capx", priority=10, owner=nodes["submitter"]
        )
        orphan = _new_task("orphan", "capx", priority=0)

        first = Scheduler.claim_stage(worker_id, capability="capx")
        assert first["task_id"] == orphan, (
            "a high-priority owner-restricted stage is SKIPPED, not a head-of-line blocker"
        )
        assert owned_by_other not in (first["task_id"],)


# ── task-simple queued (API layer) ──────────────────────────────

_SIMPLE = {"capability": "capx", "payload": {"q": "hello"}}


def _post_simple(token: str, **overrides):
    body = {**_SIMPLE, **overrides}
    return client.post(
        BASE + "/task-simple",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


class TestTaskSimpleQueued:
    def test_accepted_while_capability_busy(self):
        nodes = _two_nodes("capx")
        _set_worker_busy(nodes["worker"], busy=True)

        r = _post_simple(nodes["submitter_token"])
        assert r.status_code == 200, "was: hard 503 while capability busy — now queued"
        assert r.json()["status"] == "pending"

    def test_task_simple_defaults_to_top_priority(self):
        nodes = _two_nodes("capx")
        normal = _new_task("normal", "capx", priority=5)

        r = _post_simple(nodes["submitter_token"])
        assert r.status_code == 200
        simple_id = r.json()["task_id"]
        assert _task_priority(simple_id) == 10, (
            "task-simple without explicit priority = TOP priority (10)"
        )
        # claim order: the queued-at-top simple task beats the earlier
        # priority-5 normal task despite being created later
        node_id = nodes["worker"]
        claimed_first = Scheduler.claim_stage(node_id, capability="capx")["task_id"]
        assert claimed_first == simple_id
        claimed_second = Scheduler.claim_stage(node_id, capability="capx")["task_id"]
        assert claimed_second == normal

    def test_task_simple_explicit_priority_override(self):
        nodes = _two_nodes("capx")

        r = _post_simple(nodes["submitter_token"], priority=2)
        assert r.status_code == 200
        assert _task_priority(r.json()["task_id"]) == 2, (
            "an explicit priority wins over the default (explicit consumer choice)"
        )

    def test_404_unknown_capability_unchanged(self):
        nodes = _two_nodes("capx")

        r = client.post(
            BASE + "/task-simple",
            json={"capability": "no.such.capability", "payload": {}},
            headers={"Authorization": "Bearer " + nodes["submitter_token"]},
        )
        assert r.status_code == 404 and "no.such.capability" in r.json()["detail"]

    def test_queued_simple_work_is_claimable_when_capability_freed(self):
        nodes = _two_nodes("capx")
        _set_worker_busy(nodes["worker"], busy=True)

        r = _post_simple(nodes["submitter_token"])
        assert r.status_code == 200
        task_id = r.json()["task_id"]

        _worker = nodes["worker"]
        assert Scheduler.claim_stage(_worker, capability="capx") is None, (
            "while the capability is down, nothing claims the queued stage"
        )

        _set_worker_busy(_worker, busy=False)
        claimed = Scheduler.claim_stage(_worker, capability="capx")
        assert claimed is not None and claimed["task_id"] == task_id, (
            "once the capability is back, the queued task-simple is first in line"
        )