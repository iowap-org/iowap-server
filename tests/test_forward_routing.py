"""T-Forward: submitter- + slot-numbered ``forward`` routing (acceptance tests).

Semantics (design ``.hermes/pipeline/t-forward/design.md`` §8):
a stage with capability ``forward`` carrying ``payload["forward_slot"]`` is
resolved at submit time against the submitter's ``(node_id, slot) →
target_capability`` mapping and rewritten in place — one task ID, no
re-submission. ``submitted_by`` stays the originating node (T-227,
server-derived, anti-spoof); ``owner_node_id`` flips to the claiming node of
the single-stage rewritten task. Unmapped/unknown ``(node, slot)`` pairs are
refused with a clear 4xx — never a silently pending stage. Only nodes
advertising exactly the resolved target capability may claim the rewritten
stage. Refused slot submissions emit a ``forward_refused`` SSE event.

Plan: ``.hermes/plans/2026-10-18_forward-routing-server-dashboard.md``.
Fixture pattern follows tests/test_t229_priority_claim.py (fresh_db autouse
fixture, TestClient, RELAY_DB_PATH="" env reset).
"""

import json
import os
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError as SaIntegrityError

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
    """Use a temporary database per test and reset the event-bus history."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "forward.db"
        # Pin the defaults so register/approve chains stay non-flaky
        # (see tests/test_t229_priority_claim.py fresh_db comment).
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
            "node_name": "Admin Forward",
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


def _admin() -> str:
    _admin_id, admin_token = _register_admin(_seed_admin())
    return admin_token


def _register_and_approve(admin_token: str, name: str, caps: list) -> tuple[str, str]:
    """Register + approve one worker advertising exactly `caps`."""
    node_id, _pending = _register_worker(name, caps)
    token = _approve(admin_token, node_id, caps)
    return node_id, token


def _register_capability(admin_token: str, cap: str) -> str:
    """Make `cap` known to the capability catalog via a registered provider node."""
    node_id, _token = _register_and_approve(
        admin_token, "provider-" + cap.replace(".", "-"), [{"name": cap, "version": "1.0.0"}]
    )
    return node_id


def _put_mapping(admin_token: str, node_id: str, slot: str, target: str) -> Any:
    return client.put(
        f"/relay/v2/forward-mappings/{node_id}/{urllib.parse.quote(slot, safe='')}",
        json={"target_capability": target},
        headers={"Authorization": f"Bearer {admin_token}"},
    )


def _get_mappings(admin_token: str, node_id: str | None = None) -> Any:
    url = "/relay/v2/forward-mappings"
    if node_id is not None:
        url += f"?node={node_id}"
    return client.get(url, headers={"Authorization": f"Bearer {admin_token}"})


def _del_mapping(admin_token: str, node_id: str, slot: str) -> Any:
    return client.delete(
        f"/relay/v2/forward-mappings/{node_id}/{urllib.parse.quote(slot, safe='')}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )


def _seed_node(node_id: str, capabilities: list[str] | None = None) -> None:
    """Direct-DB node seed (test_t227 pattern) for schema-level assertions."""
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


def _forward_submit(token: str, payload: dict) -> Any:
    return client.post(
        BASE + "/task-simple",
        json={"capability": "forward", "payload": payload},
        headers={"Authorization": f"Bearer {token}"},
    )


def _stage_row(task_id: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        q(
            "SELECT capability, forward_slot, payload, status FROM task_stages"
            " WHERE task_id = ?",
            (task_id,),
        )
    ).fetchone()
    conn.close()
    return row


def _task_row(task_id: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        q("SELECT submitted_by, owner_node_id FROM tasks WHERE task_id = ?", (task_id,))
    ).fetchone()
    conn.close()
    return row


def _task_count() -> int:
    conn = get_conn()
    row = conn.execute(q("SELECT COUNT(*) AS n FROM tasks")).fetchone()
    conn.close()
    return row["n"]


def _events_of_type(etype: str) -> list:
    return [e for e in event_bus.recent(200) if e.get("type") == etype]


# ── Refusals (T-1, T-6) ─────────────────────────────────────────


class TestForwardRefusals:
    def test_forward_submit_without_mapping_refused(self):
        admin_token = _admin()
        submitter_id, submitter_token = _register_and_approve(
            admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
        )

        before = _task_count()
        r = client.post(
            BASE + "/tasks",
            json={
                "task_name": "unmapped-forward",
                "stages": [
                    {
                        "stage_name": "s1",
                        "capability": "forward",
                        "payload": {"forward_slot": "1"},
                    }
                ],
            },
            headers={"Authorization": f"Bearer {submitter_token}"},
        )
        assert r.status_code == 422, "unmapped (node, slot) forward submit must be refused"
        assert "forward_mapping_missing" in r.json()["detail"]
        assert _task_count() == before, "a refused forward submit must not create any task"
        refused = _events_of_type("forward_refused")
        assert refused and refused[-1]["payload"]["reason"] == "forward_mapping_missing"
        assert refused[-1]["payload"]["node_id"] == submitter_id

    def test_forward_submit_invalid_slot_refused(self):
        admin_token = _admin()
        submitter_id, submitter_token = _register_and_approve(
            admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
        )
        _register_capability(admin_token, "agent.ai")
        assert _put_mapping(admin_token, submitter_id, "1", "agent.ai").status_code == 200

        cases = [
            {},                        # forward_slot key missing
            {"forward_slot": ""},      # empty
            {"forward_slot": "   "},   # whitespace only
            {"forward_slot": "1 2"},   # inner whitespace
            {"forward_slot": "a" * 65},  # oversized (> 64)
            {"forward_slot": True},    # bool never coerces
        ]
        for payload in cases:
            before = _task_count()
            r = _forward_submit(submitter_token, payload)
            assert r.status_code == 422, f"invalid slot payload must be refused: {payload!r}"
            assert "forward_mapping_invalid" in r.json()["detail"], f"code mismatch: {payload!r}"
            assert _task_count() == before, f"no task for invalid slot: {payload!r}"

        # Positive control: int slot 1 normalizes to "1" and resolves the
        # existing mapping (payload value int, normalized to the "1" row).
        r = _forward_submit(submitter_token, {"forward_slot": 1})
        assert r.status_code == 200
        assert r.json()["capability"] == "agent.ai"


# ── Redirect (T-2, T-3) ─────────────────────────────────────────


class TestForwardRedirect:
    def test_forward_submit_rewrites_to_target_and_is_claimable(self):
        admin_token = _admin()
        submitter_id, submitter_token = _register_and_approve(
            admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
        )
        agent_id, agent_token = _register_and_approve(
            admin_token, "agent", [{"name": "agent.ai", "version": "1.0.0"}]
        )

        r = _put_mapping(admin_token, submitter_id, "1", "agent.ai")
        assert r.status_code == 200, "known target capability must accept the mapping"

        r = _forward_submit(submitter_token, {"forward_slot": "1"})
        assert r.status_code == 200
        body = r.json()
        assert body["capability"] == "agent.ai", "response echoes the RESOLVED target"
        task_id = body["task_id"]

        row = _stage_row(task_id)
        assert row["capability"] == "agent.ai", "stage was rewritten to the target"
        assert row["forward_slot"] == "1", "slot provenance stored on the stage"
        assert "forward_slot" not in json.loads(row["payload"]), (
            "the claiming node must not receive the stray forward_slot payload key"
        )
        assert row["status"] == "pending"
        task = _task_row(task_id)
        assert task["submitted_by"] == submitter_id, "sender stays the submitter (T-227)"
        assert task["owner_node_id"] is None, "owner is still unset before the claim"

        claim = client.post(
            BASE + "/claim",
            json={"capability": "agent.ai"},
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert claim.status_code == 200 and claim.json()["claimed"] is True
        task = _task_row(task_id)
        assert task["owner_node_id"] == agent_id, "owner flips to the claiming node"

    def test_forward_mapping_per_node_resolution(self):
        admin_token = _admin()
        node_a, token_a = _register_and_approve(
            admin_token, "submitter-a", [{"name": "submit.misc", "version": "1.0.0"}]
        )
        node_b, token_b = _register_and_approve(
            admin_token, "submitter-b", [{"name": "submit.misc", "version": "1.0.0"}]
        )
        _register_capability(admin_token, "agent.ai")
        _register_capability(admin_token, "image.generate.ai")

        assert _put_mapping(admin_token, node_a, "1", "agent.ai").status_code == 200
        assert _put_mapping(admin_token, node_b, "1", "image.generate.ai").status_code == 200

        r = _forward_submit(token_a, {"forward_slot": "1"})
        assert r.status_code == 200
        assert _stage_row(r.json()["task_id"])["capability"] == "agent.ai", (
            "node A slot 1 resolves to its own mapping"
        )
        r = _forward_submit(token_b, {"forward_slot": "1"})
        assert r.status_code == 200
        assert _stage_row(r.json()["task_id"])["capability"] == "image.generate.ai", (
            "node B slot 1 resolves to its OWN mapping, not node A's"
        )


# ── Claim semantics (T-4, T-5) ──────────────────────────────────


class TestClaimSemantics:
    def test_forward_owner_on_claim_and_submitted_by_immutable(self):
        admin_token = _admin()
        submitter_id, submitter_token = _register_and_approve(
            admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
        )
        agent_id, agent_token = _register_and_approve(
            admin_token, "agent", [{"name": "agent.ai", "version": "1.0.0"}]
        )
        assert _put_mapping(admin_token, submitter_id, "1", "agent.ai").status_code == 200

        # Anti-spoof: a client-provided submitted_by key in the payload must
        # never enter the task record — the submitter is the auth context.
        r = _forward_submit(
            submitter_token, {"forward_slot": "1", "submitted_by": "evil"}
        )
        assert r.status_code == 200
        task_id = r.json()["task_id"]

        claim = client.post(
            BASE + "/claim",
            json={"capability": "agent.ai"},
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert claim.status_code == 200 and claim.json()["claimed"] is True
        task = _task_row(task_id)
        assert task["submitted_by"] == submitter_id, "submitted_by is server-derived, immutable"
        assert task["submitted_by"] != "evil"
        assert task["owner_node_id"] == agent_id

        # Stage gone (claimed) — a second claim finds nothing claimable.
        second = client.post(
            BASE + "/claim",
            json={"capability": "agent.ai"},
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert second.status_code == 200 and second.json()["claimed"] is False

    def test_forward_claim_requires_matching_capability(self):
        admin_token = _admin()
        submitter_id, submitter_token = _register_and_approve(
            admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
        )
        misfit_id, misfit_token = _register_and_approve(
            admin_token, "misfit", [{"name": "other.cap", "version": "1.0.0"}]
        )
        agent_id, agent_token = _register_and_approve(
            admin_token, "agent", [{"name": "agent.ai", "version": "1.0.0"}]
        )
        assert _put_mapping(admin_token, submitter_id, "s-2", "agent.ai").status_code == 200

        r = _forward_submit(submitter_token, {"forward_slot": "s-2"})
        assert r.status_code == 200
        task_id = r.json()["task_id"]

        misfit = client.post(
            BASE + "/claim",
            json={"capability": "agent.ai"},
            headers={"Authorization": f"Bearer {misfit_token}"},
        )
        misfit_id  # noqa: B018 — registered to prove a non-provider node exists
        assert misfit.status_code == 200 and misfit.json()["claimed"] is False, (
            "a node not advertising the resolved target capability is refused"
        )
        agent = client.post(
            BASE + "/claim",
            json={"capability": "agent.ai"},
            headers={"Authorization": f"Bearer {agent_token}"},
        )
        assert agent.status_code == 200 and agent.json()["claimed"] is True
        assert _task_row(task_id)["owner_node_id"] == agent_id


# ── Mapping API (T-7, T-8) ──────────────────────────────────────


class TestMappingApi:
    def test_mapping_put_get_roundtrip(self):
        admin_token = _admin()
        submitter_id, _submitter_token = _register_and_approve(
            admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
        )
        _register_capability(admin_token, "agent.ai")
        other_id, _other_token = _register_and_approve(
            admin_token, "other", [{"name": "other.cap", "version": "1.0.0"}]
        )

        created = json.loads(
            _put_mapping(admin_token, submitter_id, "a", "agent.ai").content.decode()
        )
        assert set(created) == {
            "node_id", "slot", "target_capability", "created_at", "updated_at",
        }
        assert created["node_id"] == submitter_id
        assert created["slot"] == "a"
        assert created["target_capability"] == "agent.ai"

        got = _get_mappings(admin_token, node_id=submitter_id)
        assert got.status_code == 200
        rows = got.json()["mappings"]
        assert len(rows) == 1 and rows[0] == created

        # Upsert the same (node, slot): target updated, created_at kept,
        # updated_at bumped.
        updated = json.loads(
            _put_mapping(admin_token, submitter_id, "a", "other.cap").content.decode()
        )
        assert updated["target_capability"] == "other.cap"
        assert updated["created_at"] == created["created_at"]
        assert updated["updated_at"] > created["updated_at"]

        assert _get_mappings(admin_token, node_id=other_id).json()["mappings"] == []
        listed = _get_mappings(admin_token).json()["mappings"]
        assert updated in listed, "unfiltered GET returns every mapping incl. this one"

    def test_mapping_delete(self):
        admin_token = _admin()
        submitter_id, _submitter_token = _register_and_approve(
            admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
        )
        _register_capability(admin_token, "agent.ai")
        assert _put_mapping(admin_token, submitter_id, "1", "agent.ai").status_code == 200

        r = _del_mapping(admin_token, submitter_id, "1")
        assert r.status_code == 200
        assert r.json()["deleted"] is True
        assert _get_mappings(admin_token, node_id=submitter_id).json()["mappings"] == []

        again = _del_mapping(admin_token, submitter_id, "1")
        assert again.status_code == 404
        assert "forward_mapping_missing" in again.json()["detail"]

    def test_mapping_put_validation(self):
        admin_token = _admin()
        submitter_id, _submitter_token = _register_and_approve(
            admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
        )
        _register_capability(admin_token, "agent.ai")

        # Empty body {} → 422 (DTO missing-field validation runs before the
        # handler; the DTO's 422 IS the forward_mapping_invalid shape at the
        # plan level — the ladder itself raises the code for values that
        # pass the DTO).
        r = client.put(
            f"/relay/v2/forward-mappings/{submitter_id}/1",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 422, "empty body must be refused"

        # Unknown submitter node → 404 forward_node_unknown
        r = _put_mapping(admin_token, "GHOST-NODE", "1", "agent.ai")
        assert r.status_code == 404
        assert "forward_node_unknown" in r.json()["detail"]

        # Target == forward → 400 redirect_self (trivial loop refused)
        r = _put_mapping(admin_token, submitter_id, "1", "forward")
        assert r.status_code == 400
        assert "redirect_self" in r.json()["detail"]

        # Unknown target capability → 422 target_unknown_capability
        r = _put_mapping(admin_token, submitter_id, "1", "no.such.capability")
        assert r.status_code == 422
        assert "target_unknown_capability" in r.json()["detail"]

        # Whitespace-only target passes the DTO min-length and must be
        # caught by the validation ladder with the exact code.
        r = client.put(
            f"/relay/v2/forward-mappings/{submitter_id}/1",
            json={"target_capability": "   "},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 422
        assert "forward_mapping_invalid" in r.json()["detail"]

        # Non-string target (int) → 422 (invalid; DTO or ladder).
        r = client.put(
            f"/relay/v2/forward-mappings/{submitter_id}/1",
            json={"target_capability": 123},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 422

        # Invalid slot in the URL path (" a b ") → 422 forward_mapping_invalid
        r = _put_mapping(admin_token, submitter_id, " a b ", "agent.ai")
        assert r.status_code == 422
        assert "forward_mapping_invalid" in r.json()["detail"]

        # Unknown node filter on GET → 404 forward_node_unknown
        got = _get_mappings(admin_token, node_id="GHOST-NODE")
        assert got.status_code == 404
        assert "forward_node_unknown" in got.json()["detail"]


# ── Schema (plan §DB) ───────────────────────────────────────────


class TestSchema:
    def test_migration_creates_forward_mappings_and_forward_slot(self):
        conn = get_conn()
        cols = [
            r[1]
            for r in conn.exec_driver_sql(
                "PRAGMA table_info(forward_mappings)"
            ).fetchall()
        ]
        assert set(cols) == {
            "mapping_id", "node_id", "slot", "target_capability",
            "created_at", "updated_at",
        }

        _seed_node("mnode", capabilities=["agent.ai"])
        conn.execute(
            q(
                "INSERT INTO forward_mappings"
                " (mapping_id, node_id, slot, target_capability, created_at, updated_at)"
                " VALUES ('fwdm_t1', ?, '1', 'agent.ai', '2026-01-01T00:00:00+00:00',"
                " '2026-01-01T00:00:00+00:00')",
                ("mnode",),
            )
        )
        conn.commit()
        # UNIQUE(node_id, slot): per-node slot identity
        with pytest.raises(SaIntegrityError):
            conn.execute(
                q(
                    "INSERT INTO forward_mappings"
                    " (mapping_id, node_id, slot, target_capability, created_at, updated_at)"
                    " VALUES ('fwdm_t2', ?, '1', 'other.cap', '2026-01-01T00:00:00+00:00',"
                    " '2026-01-01T00:00:00+00:00')",
                    ("mnode",),
                )
            )
        # FK: unknown node is refused (foreign_keys=ON)
        with pytest.raises(SaIntegrityError):
            conn.execute(
                q(
                    "INSERT INTO forward_mappings"
                    " (mapping_id, node_id, slot, target_capability, created_at, updated_at)"
                    " VALUES (?, 'GHOST', '1', 'agent.ai', '2026-01-01T00:00:00+00:00',"
                    " '2026-01-01T00:00:00+00:00')",
                    ("fwdm_t3",),
                )
            )

        stage_cols = [
            r[1]
            for r in conn.exec_driver_sql("PRAGMA table_info(task_stages)").fetchall()
        ]
        assert "forward_slot" in stage_cols

        versions = {
            r["version"]
            for r in conn.execute(q("SELECT version FROM schema_version")).fetchall()
        }
        assert {13, 14} <= versions
        conn.close()

def _declare_and_list_slots(admin_token: str, node_id: str, token: str) -> list:
    slots = [
        {"slot": "1", "label": "Maintenance Decision", "description": "LLM-Entscheidung"},
        {"slot": "2", "label": "Image Classification", "description": "classify.ai"},
    ]
    r = client.post(
        "/relay/v2/discovery/worker-heartbeat",
        json={"forward_slots": slots},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text

    listed = client.get(
        f"/relay/v2/forward-mappings/slots?node={node_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert listed.status_code == 200, listed.text
    return listed.json()["slots"]


def test_forward_slots_declared_in_heartbeat_roundtrip():
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "fwd-slots-node", [{"name": "capx", "version": "1.0.0"}]
    )

    slots = _declare_and_list_slots(admin_token, node_id, token)
    assert [s["slot"] for s in slots] == ["1", "2"]
    assert slots[0]["label"] == "Maintenance Decision"
    assert slots[1]["label"] == "Image Classification"


def test_forward_slots_replace_on_new_heartbeat():
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "fwd-slots-repl", [{"name": "capx", "version": "1.0.0"}]
    )

    _declare_and_list_slots(admin_token, node_id, token)

    r = client.post(
        "/relay/v2/discovery/worker-heartbeat",
        json={"forward_slots": [{"slot": "1", "label": "Changed", "description": "new"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text

    listed = client.get(
        f"/relay/v2/forward-mappings/slots?node={node_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert listed.status_code == 200
    slots = listed.json()["slots"]
    assert [s["slot"] for s in slots] == ["1"]
    assert slots[0]["label"] == "Changed"
    assert slots[0]["description"] == "new"


def test_forward_slots_15_migration_persists():
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "fwd-slots-mv", [{"name": "capx", "version": "1.0.0"}]
    )

    _declare_and_list_slots(admin_token, node_id, token)

    conn = get_conn()
    table_names = {r[0] for r in conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "forward_slots" in table_names
    versions = {r["version"] for r in conn.exec_driver_sql(
        "SELECT version FROM schema_version"
    ).fetchall()}
    conn.close()
    assert 15 in versions
