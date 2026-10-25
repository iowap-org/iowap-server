"""T-246: forward-mapping pins — admin-pinned execution address (acceptance tests).

Semantics (design ``.hermes/pipeline/t246-forward-pin/design.md`` §2-§7):
``forward_mappings.target_node_id`` pins a mapping to ONE node (NULL = legacy
"any capable provider"); the submit-time rewrite copies the pin onto the stage
(``task_stages.target_node_id``); the claim gate accepts pinned stages only from
the pin target; node-delete unpins (mapping + stage level); dead pin targets
are failed at claim time (T-067 analog, pre-SELECT sweep). PUT-time validation
of a pin is hard: the pin node must exist (422 target_node_unknown) and must
advertise exactly the mapping's target_capability itself (422
target_node_lacks_capability — node-scoped row query, never the aggregate).

Unpinned behavior (all 6 live mappings today) is frozen unchanged by tests
29/30. Plan: ``.hermes/plans/2026-10-25_t246-forward-pin.md``.
Fixture pattern follows tests/test_forward_routing.py (fresh_db autouse,
TestClient, RELAY_DB_PATH="" env reset).
"""

import json
import os
import sqlite3
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

import pydantic
import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.api.v2.auth import limiter as auth_limiter
from relay_server.config import settings
from relay_server.core.auth import generate_secret, hash_secret
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.core.tables import metadata
from relay_server.main import app

BASE = "/relay/v2/scheduler"


# Copied from tests/test_forward_routing.py:42-58 (verbatim).
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


# Copied from tests/test_forward_routing.py:58 (verbatim).
client = TestClient(app)


# Copied from tests/test_forward_routing.py:61-78 (verbatim).
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


# Copied from tests/test_forward_routing.py:81-92 (verbatim).
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


# Copied from tests/test_forward_routing.py:95-107 (verbatim).
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


# Copied from tests/test_forward_routing.py:110-117 (verbatim; dependency of
# _register_and_approve, which the stubs.md helper table cites).
def _approve(admin_token: str, worker_id: str, caps: list) -> str:
    r = client.post(
        f"/relay/v2/admin/nodes/{worker_id}/approve",
        json={"role": "service", "capabilities": caps},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    return r.json()["token"]


# Copied from tests/test_forward_routing.py:120-122 (verbatim).
def _admin() -> str:
    _admin_id, admin_token = _register_admin(_seed_admin())
    return admin_token


# Copied from tests/test_forward_routing.py:125-129 (verbatim).
def _register_and_approve(admin_token: str, name: str, caps: list) -> tuple[str, str]:
    """Register + approve one worker advertising exactly `caps`."""
    node_id, _pending = _register_worker(name, caps)
    token = _approve(admin_token, node_id, caps)
    return node_id, token


# Copied from tests/test_forward_routing.py:132-137 (verbatim).
def _register_capability(admin_token: str, cap: str) -> str:
    """Make `cap` known to the capability catalog via a registered provider node."""
    node_id, _token = _register_and_approve(
        admin_token, "provider-" + cap.replace(".", "-"), [{"name": cap, "version": "1.0.0"}]
    )
    return node_id


# Copied from tests/test_forward_routing.py:140-145 (verbatim; 3-arg legacy).
def _put_mapping(admin_token: str, node_id: str, slot: str, target: str) -> Any:
    return client.put(
        f"/relay/v2/forward-mappings/{node_id}/{urllib.parse.quote(slot, safe='')}",
        json={"target_capability": target},
        headers={"Authorization": f"Bearer {admin_token}"},
    )


# Copied from tests/test_forward_routing.py:148-152 (verbatim).
def _get_mappings(admin_token: str, node_id: str | None = None) -> Any:
    url = "/relay/v2/forward-mappings"
    if node_id is not None:
        url += f"?node={node_id}"
    return client.get(url, headers={"Authorization": f"Bearer {admin_token}"})


# Copied from tests/test_forward_routing.py:155-159 (verbatim).
def _del_mapping(admin_token: str, node_id: str, slot: str) -> Any:
    return client.delete(
        f"/relay/v2/forward-mappings/{node_id}/{urllib.parse.quote(slot, safe='')}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )


# Copied from tests/test_forward_routing.py:162-174 (verbatim).
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


# Copied from tests/test_forward_routing.py:177-182 (verbatim).
def _forward_submit(token: str, payload: dict) -> Any:
    return client.post(
        BASE + "/task-simple",
        json={"capability": "forward", "payload": payload},
        headers={"Authorization": f"Bearer {token}"},
    )


# Copied from tests/test_forward_routing.py:185-195 (verbatim, legacy pair).
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


# T-246 variant of tests/test_forward_routing.py:185-195 (stubs.md §1.4:
# extended SELECT with target_node_id for the pin-asserting tests 15/16/17).
def _stage_row_pin(task_id: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        q(
            "SELECT capability, forward_slot, target_node_id, payload, status"
            " FROM task_stages WHERE task_id = ?",
            (task_id,),
        )
    ).fetchone()
    conn.close()
    return row


# Copied from tests/test_forward_routing.py:198-204 (verbatim).
def _task_row(task_id: str) -> dict:
    conn = get_conn()
    row = conn.execute(
        q("SELECT submitted_by, owner_node_id FROM tasks WHERE task_id = ?", (task_id,))
    ).fetchone()
    conn.close()
    return row


# Copied from tests/test_forward_routing.py:207-211 (verbatim).
def _task_count() -> int:
    conn = get_conn()
    row = conn.execute(q("SELECT COUNT(*) AS n FROM tasks")).fetchone()
    conn.close()
    return row["n"]


# Copied from tests/test_forward_routing.py:214-215 (verbatim).
def _events_of_type(etype: str) -> list:
    return [e for e in event_bus.recent(200) if e.get("type") == etype]


def _put_mapping_pin(admin_token: str, node_id: str, slot: str, target: str,
                     target_node_id: str | None) -> Any:
    """PUT a mapping with an explicit pin form (T-246); None → field omitted."""
    body: dict = {"target_capability": target}
    if target_node_id is not None:
        body["target_node_id"] = target_node_id
    return client.put(
        f"/relay/v2/forward-mappings/{node_id}/{urllib.parse.quote(slot, safe='')}",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )


def _post_task(token: str, stages: list[dict]) -> Any:
    """Multi-stage POST /relay/v2/scheduler/tasks (test_forward_routing BASE)."""
    return client.post(
        BASE + "/tasks",
        json={"task_name": "t246", "stages": stages},
        headers={"Authorization": f"Bearer {token}"},
    )


def _declare_slot(token: str, slots: list[dict]) -> Any:
    """Worker heartbeat declaring forward slots (tests/test_t249_...:92-97 pattern)."""
    return client.post(
        "/relay/v2/discovery/worker-heartbeat",
        json={"forward_slots": slots},
        headers={"Authorization": f"Bearer {token}"},
    )


def _raw_conn() -> sqlite3.Connection:
    """Raw sqlite3 handle to the SAME file — FK pragma OFF (default).

    Produces the orphan-row shape the live database carries (design §8.2
    test 13 arrangement); the app-path connections keep FK ON
    (core/db.py:242).
    """
    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _audit_upsert_rows() -> list:
    """Read forward.mapping_upsert audit rows from the audit_logs table."""
    conn = get_conn()
    rows = conn.execute(
        q("SELECT details FROM audit_logs WHERE action = 'forward.mapping_upsert'")
    ).fetchall()
    conn.close()
    return rows


# Assert on CONTAINMENT over the row set, not positional order (test 14).


# ── Migration + schema (T-246 design §8.1 — tests 1-5) ─────────────


def test_migration_17_adds_pin_columns():
    """Test 1 — migration 17 adds target_node_id to both tables and registers version 17."""
    conn = get_conn()
    mapping_cols = {
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(forward_mappings)").fetchall()
    }
    stage_cols = {
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(task_stages)").fetchall()
    }
    versions = {
        r["version"]
        for r in conn.execute(q("SELECT version FROM schema_version")).fetchall()
    }
    conn.close()
    assert "target_node_id" in mapping_cols, (
        "migration 17 must add forward_mappings.target_node_id"
    )
    assert "target_node_id" in stage_cols, (
        "migration 17 must add task_stages.target_node_id"
    )
    assert 17 in versions, "migration 17 must register itself in schema_version"


def test_tables_metadata_declares_pin_columns():
    """Test 2 — sa metadata declares the pin columns with FK constraints (T-245 test-2 pattern)."""
    md_maps = set(metadata.tables["forward_mappings"].columns.keys())
    md_stages = set(metadata.tables["task_stages"].columns.keys())
    conn = get_conn()
    db_maps = {
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(forward_mappings)").fetchall()
    }
    db_stages = {
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(task_stages)").fetchall()
    }
    conn.close()
    assert md_maps == db_maps, "metadata declaration must mirror the migrated table"
    assert md_stages == db_stages, "metadata declaration must mirror the migrated table"
    for table_name in ("forward_mappings", "task_stages"):
        # SQLAlchemy 2.0.52 reality (probed in this run): ForeignKeyConstraint
        # .referred_table is the referred TABLE OBJECT, not a str — resolve
        # its name so the frozen ("nodes", …) shape form holds on both.
        fk_shapes = [
            (
                tuple(fk.columns.keys()),
                getattr(fk.referred_table, "name", fk.referred_table),
                tuple(el.target_fullname for el in fk.elements),
            )
            for fk in metadata.tables[table_name].foreign_key_constraints
        ]
        assert (
            ("target_node_id",),
            "nodes",
            ("nodes.node_id",),
        ) in fk_shapes, f"{table_name} must carry the pin FK to nodes.node_id"


def test_pin_columns_carry_fk_to_nodes():
    """Test 3 — both pin columns carry an FK to nodes.node_id (PRAGMA foreign_key_list)."""
    conn = get_conn()
    map_fks = {
        (r[2], r[3])
        for r in conn.exec_driver_sql("PRAGMA foreign_key_list(forward_mappings)").fetchall()
    }
    stage_fks = {
        (r[2], r[3])
        for r in conn.exec_driver_sql("PRAGMA foreign_key_list(task_stages)").fetchall()
    }
    conn.close()
    assert ("nodes", "target_node_id") in map_fks, (
        "forward_mappings.target_node_id must reference nodes(node_id)"
    )
    assert ("nodes", "target_node_id") in stage_fks, (
        "task_stages.target_node_id must reference nodes(node_id)"
    )


def test_migration_17_idempotent_on_replay():
    """Test 4 — _m017 called twice on one connection: each column appears exactly once.

    Phase causes (design §8): pre-scaffold the in-body import is an
    ImportError (symbol missing); post-scaffold (pass-stub) the calls run
    but the columns stay absent → the count assertions fail; post-coder GREEN.
    """
    from relay_server.core.db import _m017_forward_mapping_pins

    conn = get_conn()
    _m017_forward_mapping_pins(conn)
    _m017_forward_mapping_pins(conn)
    map_names = [
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(forward_mappings)").fetchall()
    ]
    stage_names = [
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(task_stages)").fetchall()
    ]
    conn.close()
    assert map_names.count("target_node_id") == 1, (
        "replayed migration must leave exactly one forward_mappings.target_node_id"
    )
    assert stage_names.count("target_node_id") == 1, (
        "replayed migration must leave exactly one task_stages.target_node_id"
    )


def test_pin_columns_are_nullable():
    """Test 5 — a pre-pin 6-column INSERT writes NULL pins (nullable, no backfill)."""
    _seed_node("mnode", capabilities=["agent.ai"])
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO forward_mappings"
            " (mapping_id, node_id, slot, target_capability, created_at, updated_at)"
            " VALUES ('fwdm_t5', ?, '1', 'agent.ai', '2026-01-01T00:00:00+00:00',"
            " '2026-01-01T00:00:00+00:00')",
            ("mnode",),
        )
    )
    conn.commit()
    row = conn.execute(
        q("SELECT target_node_id FROM forward_mappings WHERE mapping_id = 'fwdm_t5'")
    ).fetchone()
    conn.close()
    assert row["target_node_id"] is None, "no backfill: an unpinned row reads NULL"


# ── PUT ladder (T-246 design §8.2 — tests 6-14) ────────────────────


def test_put_pin_roundtrip_and_response_shape():
    """Test 6 — pinned PUT roundtrip: PUT and GET rows carry the full 6-key shape."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token, "provider", [{"name": "agent.ai", "version": "1.0.0"}]
    )

    created = _put_mapping_pin(admin_token, submitter_id, "1", "agent.ai", provider_id)
    assert created.status_code == 200
    row = created.json()
    assert row["target_node_id"] == provider_id, "PUT response row carries the pin"
    assert set(row) == {
        "node_id", "slot", "target_capability", "target_node_id",
        "binding", "created_at", "updated_at",
    }

    got = _get_mappings(admin_token, node_id=submitter_id)
    assert got.status_code == 200
    rows = got.json()["mappings"]
    assert len(rows) == 1
    assert rows[0] == row, "GET rows identical to the PUT response row"
    assert set(rows[0]) == {
        "node_id", "slot", "target_capability", "target_node_id",
        "binding", "created_at", "updated_at",
    }


def test_put_pin_unknown_node_422():
    """Test 7 — pin to a node with no nodes row → 422 target_node_unknown."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _register_capability(admin_token, "agent.ai")

    r = _put_mapping_pin(admin_token, submitter_id, "1", "agent.ai", "NOPE1234")
    assert r.status_code == 422, r.text
    assert "target_node_unknown" in r.json()["detail"]


def test_put_pin_node_lacks_capability_422():
    """Test 8 — pin target advertising only other.cap → 422 target_node_lacks_capability.

    Anti-aggregate: P1 keeps agent.ai in the catalog — the node-scoped check
    must still refuse the pin to P2 (design §10).
    """
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _p1_id, _p1_token = _register_and_approve(
        admin_token, "p1", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    p2_id, _p2_token = _register_and_approve(
        admin_token, "p2", [{"name": "other.cap", "version": "1.0.0"}]
    )

    r = _put_mapping_pin(admin_token, submitter_id, "1", "agent.ai", p2_id)
    assert r.status_code == 422, r.text
    assert "target_node_lacks_capability" in r.json()["detail"]


def test_put_pin_form_guards_422():
    """Test 9 — non-str/over-long pin values surface as pydantic LIST validation 422s."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _register_capability(admin_token, "agent.ai")

    r_int = _put_mapping_pin(admin_token, submitter_id, "1", "agent.ai", 1)
    assert r_int.status_code == 422, r_int.text
    assert isinstance(r_int.json()["detail"], list), (
        "a model form-guard 422 must be a list, not a ladder string detail"
    )

    r_long = _put_mapping_pin(admin_token, submitter_id, "1", "agent.ai", "x" * 65)
    assert r_long.status_code == 422, r_long.text
    assert isinstance(r_long.json()["detail"], list), (
        "a model form-guard 422 must be a list, not a ladder string detail"
    )


def test_put_unpin_writes_null():
    """Test 10 — PUT with pin "" and with the field omitted both leave the row pin NULL.

    Phase causes (design §8): the direct read is an OperationalError while the
    migration body is the pass-stub (column absent); at post-coder the row
    reads NULL both times (the model field guards, the ladder writes).
    """
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token, "provider", [{"name": "agent.ai", "version": "1.0.0"}]
    )

    pinned = _put_mapping_pin(admin_token, submitter_id, "1", "agent.ai", provider_id)
    assert pinned.status_code == 200
    # Design §3.2: the pin lands on the row; post-coder the re-read shows it.
    conn = get_conn()
    pinned_row = conn.execute(
        q(
            "SELECT target_node_id FROM forward_mappings"
            " WHERE node_id = ? AND slot = '1'",
            (submitter_id,),
        )
    ).fetchone()
    conn.close()
    assert pinned_row["target_node_id"] == provider_id, (
        "the pinned PUT stores the pin (arrangement precondition)"
    )

    r1 = _put_mapping_pin(admin_token, submitter_id, "1", "agent.ai", "")
    assert r1.status_code == 200
    conn = get_conn()
    row = conn.execute(
        q(
            "SELECT target_node_id FROM forward_mappings"
            " WHERE node_id = ? AND slot = '1'",
            (submitter_id,),
        )
    ).fetchone()
    conn.close()
    assert row["target_node_id"] is None, 'PUT with pin "" must unpin'

    r2 = _put_mapping(admin_token, submitter_id, "1", "agent.ai")
    assert r2.status_code == 200
    conn = get_conn()
    row = conn.execute(
        q(
            "SELECT target_node_id FROM forward_mappings"
            " WHERE node_id = ? AND slot = '1'",
            (submitter_id,),
        )
    ).fetchone()
    conn.close()
    assert row["target_node_id"] is None, "a PUT omitting the pin must unpin"


def test_put_pin_preserves_redirect_self_400():
    """Test 11 (GUARD) — redirect_self (400) precedes the pin checks."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token, "provider", [{"name": "agent.ai", "version": "1.0.0"}]
    )

    r = _put_mapping_pin(admin_token, submitter_id, "1", "forward", provider_id)
    assert r.status_code == 400, r.text
    assert "redirect_self" in r.json()["detail"]


def test_put_unpinned_ladder_unchanged():
    """Test 12 (GUARD) — the legacy unpinned PUT ladder keeps its codes (string details)."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _register_capability(admin_token, "agent.ai")

    r = _put_mapping(admin_token, submitter_id, " a b ", "agent.ai")
    assert r.status_code == 422
    assert "forward_mapping_invalid" in r.json()["detail"]

    r = _put_mapping(admin_token, "GHOST-NODE", "1", "agent.ai")
    assert r.status_code == 404
    assert "forward_node_unknown" in r.json()["detail"]

    r = _put_mapping(admin_token, submitter_id, "1", "no.such.capability")
    assert r.status_code == 422
    assert "target_unknown_capability" in r.json()["detail"]


def test_orphan_capability_rows_cannot_green_the_pin_check():
    """Test 13 — orphan node_capabilities rows satisfy the aggregate, not the node-scoped check."""
    live = _raw_conn()
    live.execute(
        "INSERT INTO node_capabilities (node_id, capability_name, capability_type, capability_version, available, updated_at) VALUES ('ORPHAN-1', 'orphan.cap', 'tool', '1.0.0', 1, '2026-01-01T00:00:00+00:00')"
    )
    live.commit()
    live.close()
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    live_node, _live_token = _register_and_approve(
        admin_token, "live", [{"name": "other.cap", "version": "1.0.0"}]
    )

    # (a) characterization: pre-existing orphan wrong-green, plan line 36:
    # "Befund bleibt" — a future aggregate-repair card updates this half.
    a = _put_mapping(admin_token, submitter_id, "1", "orphan.cap")
    assert a.status_code == 200, (
        "the aggregate check reads the orphan row — pre-existing wrong-green"
    )

    b = _put_mapping_pin(admin_token, submitter_id, "2", "orphan.cap", live_node)
    assert b.status_code == 422, b.text
    assert "target_node_lacks_capability" in b.json()["detail"], (
        "the node-scoped check requires the pin node's OWN row"
    )


def test_put_audit_detail_carries_pin():
    """Test 14 — audit details carry the pin token (pin=<id> / pin=variabel)."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token, "provider", [{"name": "agent.ai", "version": "1.0.0"}]
    )

    pinned = _put_mapping_pin(admin_token, submitter_id, "1", "agent.ai", provider_id)
    assert pinned.status_code == 200
    details = [r["details"] for r in _audit_upsert_rows()]
    assert any(f"pin={provider_id}" in d for d in details), (
        f"the pinned PUT detail must carry pin={provider_id}"
    )

    unpinned = _put_mapping(admin_token, submitter_id, "1", "agent.ai")
    assert unpinned.status_code == 200
    details = [r["details"] for r in _audit_upsert_rows()]
    assert any("pin=variabel" in d for d in details), (
        "the unpinned PUT detail must carry pin=variabel"
    )


# ── Resolve + claim gate (T-246 design §8.3 — tests 15-22) ─────────


def test_pinned_submit_sets_stage_pin():
    """Test 15 — the submit-time rewrite copies the mapping pin onto the stage.

    Phase causes (design §8): pre-migration the extended stage read is an
    OperationalError (no column); post-coder the pin reads the provider.
    """
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token, "provider", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    assert _put_mapping_pin(
        admin_token, submitter_id, "1", "agent.ai", provider_id
    ).status_code == 200

    r = _forward_submit(submitter_token, {"forward_slot": "1"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    row = _stage_row_pin(task_id)
    assert row["capability"] == "agent.ai", "stage was rewritten to the target"
    assert row["forward_slot"] == "1", "slot provenance stored on the stage"
    assert row["target_node_id"] == provider_id, "the mapping pin travels to the stage"
    task = _task_row(task_id)
    assert task["submitted_by"] == submitter_id, "sender stays the submitter (T-227)"
    assert task["owner_node_id"] is None, "owner is still unset before the claim"


def test_unpinned_submit_keeps_stage_pin_null():
    """Test 16 — an unpinned mapping submit leaves the stage pin NULL (legacy redirect)."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _register_capability(admin_token, "agent.ai")
    assert _put_mapping(admin_token, submitter_id, "1", "agent.ai").status_code == 200

    r = _forward_submit(submitter_token, {"forward_slot": "1"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    row = _stage_row_pin(task_id)
    assert row["target_node_id"] is None, "no pin = unpinned"
    assert row["capability"] == "agent.ai", "full legacy redirect (capability)"
    assert row["forward_slot"] == "1", "full legacy redirect (slot)"
    task = _task_row(task_id)
    assert task["submitted_by"] == submitter_id, "sender stays the submitter (T-227)"
    assert task["owner_node_id"] is None, "owner is still unset before the claim"


def test_multi_stage_fanout_two_pins_two_nodes():
    """Test 17 — one task, two forward stages, pins on two different nodes (C3)."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    p1_id, _p1_token = _register_and_approve(
        admin_token, "p1", [{"name": "cap.a", "version": "1.0.0"}]
    )
    p2_id, _p2_token = _register_and_approve(
        admin_token, "p2", [{"name": "cap.b", "version": "1.0.0"}]
    )
    assert _put_mapping_pin(admin_token, submitter_id, "1", "cap.a", p1_id).status_code == 200
    assert _put_mapping_pin(admin_token, submitter_id, "2", "cap.b", p2_id).status_code == 200

    r = _post_task(
        submitter_token,
        [
            {"stage_name": "s1", "capability": "forward", "payload": {"forward_slot": "1"}},
            {"stage_name": "s2", "capability": "forward", "payload": {"forward_slot": "2"}},
        ],
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]["task_id"]

    conn = get_conn()
    rows = conn.execute(
        q(
            "SELECT stage_name, capability, forward_slot, target_node_id, status"
            " FROM task_stages WHERE task_id = ? ORDER BY sequence ASC",
            (task_id,),
        )
    ).fetchall()
    conn.close()
    by_name = {row["stage_name"]: row for row in rows}
    assert set(by_name) == {"s1", "s2"}
    assert by_name["s1"]["target_node_id"] == p1_id, "stage 1 pinned to P1"
    assert by_name["s2"]["target_node_id"] == p2_id, "stage 2 pinned to P2"
    task = _task_row(task_id)
    assert task["owner_node_id"] is None, "multi-stage tasks never flip the owner pre-claim"


def test_claim_gate_pinned_stage_only_pin_target_claims():
    """Test 18 — the claim gate accepts a pinned stage ONLY from its pin target."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _misfit_id, misfit_token = _register_and_approve(
        admin_token, "misfit", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    pin_id, pin_token = _register_and_approve(
        admin_token, "pin-provider", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    assert _put_mapping_pin(
        admin_token, submitter_id, "1", "agent.ai", pin_id
    ).status_code == 200

    r = _forward_submit(submitter_token, {"forward_slot": "1"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    misfit = client.post(
        BASE + "/claim",
        json={"capability": "agent.ai"},
        headers={"Authorization": f"Bearer {misfit_token}"},
    )
    assert misfit.status_code == 200 and misfit.json()["claimed"] is False, (
        "a non-pin node never claims a pinned stage"
    )
    winner = client.post(
        BASE + "/claim",
        json={"capability": "agent.ai"},
        headers={"Authorization": f"Bearer {pin_token}"},
    )
    assert winner.status_code == 200 and winner.json()["claimed"] is True
    row = _stage_row(task_id)
    assert row["status"] == "claimed", "the pin-node claim moved the stage to claimed"
    assert row["capability"] == "agent.ai", "the resolved target stays unchanged"


def test_claim_gate_unpinned_any_capable_node():
    """Test 19 (GUARD) — unpinned stages keep the first-claimer-wins semantics."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    a_id, a_token = _register_and_approve(
        admin_token, "capable-a", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    _b_id, _b_token = _register_and_approve(
        admin_token, "capable-b", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    assert _put_mapping(admin_token, submitter_id, "1", "agent.ai").status_code == 200

    r = _forward_submit(submitter_token, {"forward_slot": "1"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    first = client.post(
        BASE + "/claim",
        json={"capability": "agent.ai"},
        headers={"Authorization": f"Bearer {a_token}"},
    )
    assert first.status_code == 200 and first.json()["claimed"] is True
    second = client.post(
        BASE + "/claim",
        json={"capability": "agent.ai"},
        headers={"Authorization": f"Bearer {_b_token}"},
    )
    assert second.status_code == 200 and second.json()["claimed"] is False, (
        "the first capable claimer wins"
    )
    assert _task_row(task_id)["owner_node_id"] == a_id


def test_dead_pin_target_pending_stage_fails_at_claim():
    """Test 20 — dead pin targets fail pending stages at claim time (T-067 analog).

    Arrangement: raw sqlite3 (FK OFF) plants the task + pending stage with
    target_node_id='DEADNODE'; LIVE is a registered provider.
    """
    admin_token = _admin()
    _live_id, live_token = _register_and_approve(
        admin_token, "live", [{"name": "cap.c", "version": "1.0.0"}]
    )

    raw = _raw_conn()
    raw.execute(
        "INSERT INTO tasks (task_id, task_name, status, created_at, updated_at)"
        " VALUES ('t-dead-1', 't246-dead-pin', 'pending',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO task_stages (stage_id, task_id, stage_name, capability, status,"
        " sequence, created_at, updated_at, target_node_id)"
        " VALUES ('st-dead-1', 't-dead-1', 's1', 'cap.c', 'pending', 0,"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'DEADNODE')"
    )
    raw.commit()
    raw.close()

    claim = client.post(
        BASE + "/claim",
        json={"capability": "cap.c"},
        headers={"Authorization": f"Bearer {live_token}"},
    )
    asserted = claim.status_code == 200 and claim.json()["claimed"] is False
    assert asserted, "the dead-pinned pending stage must not be claimable"

    conn = get_conn()
    row = conn.execute(
        q("SELECT status FROM task_stages WHERE stage_id = 'st-dead-1'")
    ).fetchone()
    conn.close()
    assert row["status"] == "failed", "the dead pin target fails the stage (pre-SELECT sweep)"


def test_claim_response_shape_unchanged():
    """Test 21 (GUARD) — the claim payload keeps the legacy StageSummary keys (D-09)."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, provider_token = _register_and_approve(
        admin_token, "provider", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    assert _put_mapping_pin(
        admin_token, submitter_id, "1", "agent.ai", provider_id
    ).status_code == 200

    r = _forward_submit(submitter_token, {"forward_slot": "1"})
    assert r.status_code == 200

    claim = client.post(
        BASE + "/claim",
        json={"capability": "agent.ai"},
        headers={"Authorization": f"Bearer {provider_token}"},
    )
    assert claim.status_code == 200 and claim.json()["claimed"] is True
    keys = set(claim.json()["stage"].keys())
    assert "target_node_id" not in keys, "claim payload untouched (no pin exposure)"
    assert {
        "stage_id", "task_id", "stage_name", "capability", "status", "depends_on",
        "claimed_by", "claimed_at", "completed_at", "payload", "forward_slot",
        "result", "retry_count", "capability_details",
    } <= keys, "legacy StageSummary keys intact"


def test_slot_type_gate_precedes_pin_resolution():
    """Test 22 (GUARD) — the T-245 type gate runs before mapping/pin resolution."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token, "provider", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    _register_capability(admin_token, "agent.ai")

    hb = _declare_slot(
        submitter_token, [{"slot": "1", "label": "x", "type": "text", "value": "v"}]
    )
    assert hb.status_code == 200, hb.text

    # PUT does not consult forward_slots (D-16) — pinning a value slot is
    # possible at PUT; the submit must refuse BEFORE resolution with the
    # T-245 code, regardless of any pin.
    put = _put_mapping_pin(admin_token, submitter_id, "1", "agent.ai", provider_id)
    assert put.status_code == 200

    r = _forward_submit(submitter_token, {"forward_slot": "1"})
    assert r.status_code == 422, r.text
    assert "forward_slot_not_routable" in r.json()["detail"], (
        "the type gate precedes pin/mapping resolution"
    )


# ── Node-delete (T-246 design §8.4 — tests 23-25) ──────────────────


def test_node_delete_unpins_forward_mappings():
    """Test 23 — deleting the pin target unpins the mapping (the row survives).

    Phase causes (design §8): the direct-DB read is an OperationalError while
    the migration body is the pass-stub; post-coder the row survives unpinned.
    """
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token, "provider-x", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    assert _put_mapping_pin(
        admin_token, submitter_id, "1", "agent.ai", provider_id
    ).status_code == 200

    r = client.delete(
        f"/relay/v2/admin/nodes/{provider_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text

    conn = get_conn()
    row = conn.execute(
        q(
            "SELECT node_id, slot, target_capability, target_node_id"
            " FROM forward_mappings WHERE node_id = ?",
            (submitter_id,),
        )
    ).fetchone()
    conn.close()
    assert row is not None, "the mapping row survives the pin target's deletion"
    assert row["node_id"] == submitter_id
    assert row["slot"] == "1"
    assert row["target_capability"] == "agent.ai"
    assert row["target_node_id"] is None, "the pin reference is voided (C5)"


def test_node_delete_clears_stage_pins_fk_safe():
    """Test 24 — deleting the pin target clears the stage pin (T-249 500-regression guard).

    Phase causes (design §8): same two-phase pattern as test 23 — an
    OperationalError at the post-scaffold read; post-coder the stage survives
    pending with the pin cleared.
    """
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token, "provider-x", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    assert _put_mapping_pin(
        admin_token, submitter_id, "1", "agent.ai", provider_id
    ).status_code == 200

    r = _forward_submit(submitter_token, {"forward_slot": "1"})
    assert r.status_code == 200
    task_id = r.json()["task_id"]

    delete = client.delete(
        f"/relay/v2/admin/nodes/{provider_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert delete.status_code == 200, delete.text

    row = _stage_row_pin(task_id)
    assert row["status"] == "pending", "the pending stage continues unpinned"
    assert row["target_node_id"] is None, "stage pin cleared before the nodes DELETE"


def test_node_delete_still_removes_submitter_mappings():
    """Test 25 (GUARD) — the deleted node's OWN mappings still get removed (legacy)."""
    admin_token = _admin()
    x_id, _x_token = _register_and_approve(
        admin_token, "target-x", [{"name": "agent.ai", "version": "1.0.0"},
                                   {"name": "other.cap", "version": "1.0.0"}]
    )
    assert _put_mapping(admin_token, x_id, "1", "agent.ai").status_code == 200
    assert _put_mapping(admin_token, x_id, "2", "other.cap").status_code == 200

    r = client.delete(
        f"/relay/v2/admin/nodes/{x_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text

    conn = get_conn()
    n = conn.execute(
        q("SELECT COUNT(*) AS n FROM forward_mappings WHERE node_id = ?", (x_id,))
    ).fetchone()["n"]
    conn.close()
    assert n == 0, "the deleted node's own mappings are GONE (legacy behavior)"


# ── API forms + models (T-246 design §8.5 — tests 26-28) ───────────


def test_forward_mapping_put_model_form_guards():
    """Test 26 — ForwardMappingPut form-guards the pin (int/bool/len>64 → ValidationError)."""
    from relay_server.models import ForwardMappingPut

    default = ForwardMappingPut(target_capability="cap.c")
    assert default.target_node_id is None, "the pin default is None (unpinned)"

    with pytest.raises(pydantic.ValidationError):
        ForwardMappingPut(target_capability="cap.c", target_node_id=1)
    with pytest.raises(pydantic.ValidationError):
        ForwardMappingPut(target_capability="cap.c", target_node_id=True)
    with pytest.raises(pydantic.ValidationError):
        ForwardMappingPut(target_capability="cap.c", target_node_id="x" * 65)
    accepted = ForwardMappingPut(target_capability="cap.c", target_node_id="ABC")
    assert accepted.target_node_id == "ABC"
    accepted_none = ForwardMappingPut(target_capability="cap.c", target_node_id=None)
    assert accepted_none.target_node_id is None


def test_forward_mapping_entry_model_carries_pin():
    """Test 27 — ForwardMappingEntry round-trips the pin; default None; full key set."""
    from relay_server.models import ForwardMappingEntry

    unpinned = ForwardMappingEntry(
        node_id="S",
        slot="1",
        target_capability="agent.ai",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    assert unpinned.target_node_id is None, "the pin default is None"
    pinned = ForwardMappingEntry(
        node_id="S",
        slot="1",
        target_capability="agent.ai",
        target_node_id="P",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    assert pinned.target_node_id == "P", "the pin round-trips"
    dumped = pinned.model_dump()
    assert dumped["target_node_id"] == "P", "the pin round-trips through model_dump"
    assert set(dumped) == {
        "node_id", "slot", "target_capability", "target_node_id",
        "binding", "created_at", "updated_at",
    }


def test_get_mappings_rows_carry_pin_field():
    """Test 28 — even unpinned GET rows carry target_node_id (value None)."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _register_capability(admin_token, "agent.ai")
    assert _put_mapping(admin_token, submitter_id, "1", "agent.ai").status_code == 200

    got = _get_mappings(admin_token, node_id=submitter_id)
    assert got.status_code == 200
    rows = got.json()["mappings"]
    assert len(rows) == 1
    assert set(rows[0]) == {
        "node_id", "slot", "target_capability", "target_node_id",
        "binding", "created_at", "updated_at",
    }
    assert rows[0]["target_node_id"] is None


# ── Live-form regression (T-246 design §8.6 — tests 29-30) ─────────


def test_six_live_mappings_analog_unpinned_end_to_end():
    """Test 29 (GUARD) — the six live mappings analog: all unpinned, legacy chain intact."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _provider_id, provider_token = _register_and_approve(
        admin_token, "provider", [{"name": "agent.ai", "version": "1.0.0"}]
    )

    hb = _declare_slot(
        submitter_token,
        [
            {"slot": "1", "label": "Maintenance Decision"},
            {"slot": "2", "label": "Image Classification"},
            {"slot": "3", "label": "Third Slot"},
        ],
    )
    assert hb.status_code == 200, hb.text

    for slot in ("1", "2", "3", "a", "b", "c"):
        assert _put_mapping(admin_token, submitter_id, slot, "agent.ai").status_code == 200

    rows = _get_mappings(admin_token, node_id=submitter_id).json()["mappings"]
    assert len(rows) == 6
    for row in rows:
        assert row["target_node_id"] is None, "the analog stays unpinned"

    r = _forward_submit(submitter_token, {"forward_slot": "1"})
    assert r.status_code == 200
    body = r.json()
    assert body["capability"] == "agent.ai", "response echoes the RESOLVED target"
    task_id = body["task_id"]

    claim = client.post(
        BASE + "/claim",
        json={"capability": "agent.ai"},
        headers={"Authorization": f"Bearer {provider_token}"},
    )
    assert claim.status_code == 200 and claim.json()["claimed"] is True
    stage_id = claim.json()["stage"]["stage_id"]
    done = client.post(
        BASE + f"/stages/{stage_id}/complete",
        json={"result": {}},
        headers={"Authorization": f"Bearer {provider_token}"},
    )
    assert done.status_code == 200, done.text

    conn = get_conn()
    task = conn.execute(
        q("SELECT status FROM tasks WHERE task_id = ?", (task_id,))
    ).fetchone()
    conn.close()
    assert task["status"] == "completed", "the legacy chain completes the task"


def test_forward_refused_event_unchanged():
    """Test 30 (GUARD) — unmapped-slot submits still refuse with the same event gate."""
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
    assert len(refused) >= 1, "a forward_refused event is published (event gate unchanged)"
    assert refused[-1]["payload"]["reason"] == "forward_mapping_missing", (
        "the event gate code set is unchanged (D-04)"
    )
    assert refused[-1]["payload"]["node_id"] == submitter_id