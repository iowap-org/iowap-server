"""T-227: submitted_by (sender flag) + owner-on-claim flip (single-stage).

Semantics (Ronny, 2026-10-14 — feature-gap closure):

- ``owner`` is who receives the order. It already gates claim routing
  (T-067) and read scope (T-005g); a task submitted WITH an owner is
  invisible to every other node.
- NEW: the sender (``submitted_by``, server-derived from the auth
  context at submit time — NOT client-supplied) can read every task it
  submitted, including tasks owned by someone else.
- NEW: an ownerless single-stage task becomes owned by its claiming
  node (``owner_node_id`` = ``claimed_by``). Visibility then narrows to
  owner + sender — the broad NULL-owner read ends at the first claim.
- Multi-stage (fan-out) tasks do NOT flip: stages keep routing to any
  capable node, and the sender reads results via ``submitted_by``.
- SSE stays a cluster broadcast (separate decision, out of scope).
- Legacy tasks (pre-flag) keep owner-only semantics via NULL sender.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from relay_server.config import settings
from relay_server.core.artifacts import store_artifact
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.core.scheduler import Scheduler
from relay_server.models.task import SimpleTaskRequest, TaskRequest


@pytest.fixture(autouse=True)
def fresh_db():
    """Temporary per-test database; event-bus history reset (T-005g pattern)."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "test.db"
        settings.artifacts_dir = Path(tmp) / "artifacts"
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


def _seed_node(node_id: str, capabilities: list[str] | None = None) -> None:
    conn = get_conn()
    caps = json.dumps([{"name": c} for c in (capabilities or [])])
    conn.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, status, capabilities, registered_at, last_seen)"
            " VALUES (?, ?, 'online', ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (node_id, node_id, caps),
        )
    )
    conn.commit()
    conn.close()


def _single_stage_task(owner: str | None, sender: str | None, cap: str = "generic") -> str:
    return Scheduler.create_task(
        task_name="t227",
        stages=[{
            "stage_name": "s1",
            "capability": cap,
            "depends_on": None,
            "timeout_seconds": 60,
            "payload": {},
        }],
        owner_node_id=owner,
        submitted_by=sender,
    )["task_id"]


def _multi_stage_task(sender: str | None) -> str:
    return Scheduler.create_task(
        task_name="t227-fanout",
        stages=[
            {"stage_name": "s1", "capability": "generic",
             "depends_on": None, "timeout_seconds": 60, "payload": {}},
            {"stage_name": "s2", "capability": "generic",
             "depends_on": ["s1"], "timeout_seconds": 60, "payload": {}},
        ],
        owner_node_id=None,
        submitted_by=sender,
    )["task_id"]


# ── submitted_by read scope ──────────────────────────────────


class TestSenderReadScope:
    def test_sender_reads_task_with_foreign_owner(self):
        _seed_node("alice"); _seed_node("bob")
        tid = _single_stage_task("bob", "alice")
        # sender must see it in list AND via GET (today: 404/absent)
        assert tid in [t["task_id"] for t in Scheduler.list_tasks(owner_node_id="alice", submitted_by="alice")]
        assert Scheduler.get_task(tid, owner_node_id="alice", submitted_by="alice") is not None

    def test_owner_still_reads_owned_task(self):
        _seed_node("bob")
        tid = _single_stage_task("bob", "alice")
        assert Scheduler.get_task(tid, owner_node_id="bob", submitted_by=None) is not None

    def test_third_node_blind_on_owned_task(self):
        _seed_node("bob"); _seed_node("carl")
        tid = _single_stage_task("bob", "alice")
        assert Scheduler.get_task(tid, owner_node_id="carl", submitted_by="carl") is None
        assert tid not in [t["task_id"] for t in Scheduler.list_tasks(owner_node_id="carl", submitted_by="carl")]

    def test_admin_sees_everything(self):
        _seed_node("bob")
        tid = _single_stage_task("bob", "alice")
        assert Scheduler.get_task(tid, owner_node_id=None, submitted_by=None) is not None


# ── owner-on-claim flip (single-stage only) ──────────────────


class TestOwnerOnClaim:
    def test_claim_flips_owner_single_stage(self):
        _seed_node("bob", capabilities=["generic"]); _seed_node("carl")
        tid = _single_stage_task(None, "alice")
        stage = Scheduler.claim_stage("bob", capability="generic")
        assert stage is not None
        task = Scheduler.get_task(tid, owner_node_id=None)  # admin view
        assert task["owner_node_id"] == "bob"

    def test_claim_flip_blinds_third_nodes(self):
        _seed_node("bob", capabilities=["generic"]); _seed_node("carl")
        tid = _single_stage_task(None, "alice")
        Scheduler.claim_stage("bob", capability="generic")
        # after the flip: broad NULL-owner read ends — only owner(+sender+admin)
        assert Scheduler.get_task(tid, owner_node_id="carl", submitted_by="carl") is None
        assert tid not in [t["task_id"] for t in Scheduler.list_tasks(owner_node_id="carl", submitted_by="carl")]

    def test_sender_and_owner_still_see_after_flip(self):
        _seed_node("bob", capabilities=["generic"])
        tid = _single_stage_task(None, "alice")
        Scheduler.claim_stage("bob", capability="generic")
        assert Scheduler.get_task(tid, owner_node_id="bob", submitted_by=None) is not None
        assert Scheduler.get_task(tid, owner_node_id=None, submitted_by="alice") is not None

    def test_multistage_ownerless_does_not_flip(self):
        _seed_node("bob", capabilities=["generic"]); _seed_node("carl", capabilities=["generic"])
        tid = _multi_stage_task("alice")
        stage = Scheduler.claim_stage("bob", capability="generic")
        assert stage is not None
        task = Scheduler.get_task(tid, owner_node_id=None)
        assert task["owner_node_id"] is None  # fan-out contract intact
        # carl can still see the (unclaimed) task and take stage 2 later
        assert Scheduler.get_task(tid, owner_node_id=None, submitted_by="carl") is not None


# ── request-model surface (anti-spoof) ───────────────────────


class TestRequestModelSurface:
    def test_simple_request_carries_no_client_sender(self):
        req = SimpleTaskRequest(**{
            "capability": "generic",
            "payload": {},
            "submitted_by": "carl",  # spoof attempt
        })
        assert not hasattr(req, "submitted_by")
        assert "submitted_by" not in req.model_dump()

    def test_task_request_carries_no_client_sender(self):
        req = TaskRequest(**{
            "task_name": "t",
            "stages": [{"stage_name": "s1", "capability": "generic"}],
            "submitted_by": "carl",  # spoof attempt
        })
        assert not hasattr(req, "submitted_by")
        assert "submitted_by" not in req.model_dump()


# ── artifacts under the new predicate ────────────────────────


class TestArtifactScope:
    def test_artifacts_visible_for_sender(self):
        _seed_node("alice"); _seed_node("bob")
        tid = _single_stage_task("bob", "alice")
        _seed_node("wk")
        store_artifact(name="f.bin", content=b"d", mime_type=None, task_id=tid, stage_id=None, created_by="wk")
        items = Scheduler.list_artifacts_scoped(tid, owner_node_id="alice", submitted_by="alice")
        assert len(items) == 1

    def test_artifacts_hidden_for_third_node(self):
        _seed_node("alice"); _seed_node("bob"); _seed_node("carl")
        tid = _single_stage_task("bob", "alice")
        _seed_node("wk")
        store_artifact(name="f.bin", content=b"d", mime_type=None, task_id=tid, stage_id=None, created_by="bob")
        assert Scheduler.list_artifacts_scoped(tid, owner_node_id="carl", submitted_by="carl") == []