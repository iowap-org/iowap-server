"""T-245: typed forward slots — migration 16, declaration validation,
submit gate. RED-first test file (Phase-2 scaffold)."""

import json  # copied-helper need: _seed_node below uses json.dumps (source file imports it)
import os
import tempfile
import urllib.parse  # copied-helper need: _put_mapping below quotes the slot into the URL
from pathlib import Path
from typing import Any  # copied-helper need: _put_mapping/_forward_submit keep their annotations

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.api.v2.auth import limiter as auth_limiter
from relay_server.core.auth import generate_secret, hash_secret  # copied-helper need: _seed_admin
from relay_server.config import settings
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.core.forward import ForwardRoutingError, replace_forward_slots
from relay_server.core.tables import metadata
from relay_server.main import app

# Copied from tests/test_forward_routing.py:39 (needed by the copied
# _forward_submit helper; reused by the T-245 _submit_forward_task helper).
BASE = "/relay/v2/scheduler"


# Copied from tests/test_forward_routing.py:42-55 (fresh_db fixture, verbatim).
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


# Copied from tests/test_forward_routing.py:110-117 (verbatim).
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


# Copied from tests/test_forward_routing.py:140-145 (verbatim).
def _put_mapping(admin_token: str, node_id: str, slot: str, target: str) -> Any:
    return client.put(
        f"/relay/v2/forward-mappings/{node_id}/{urllib.parse.quote(slot, safe='')}",
        json={"target_capability": target},
        headers={"Authorization": f"Bearer {admin_token}"},
    )


# Copied from tests/test_forward_routing.py:162-174 (verbatim; unused by the
# 20 tests but part of the stubs.md helper table for T-245).
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


# Copied from tests/test_forward_routing.py:207-211 (verbatim).
def _task_count() -> int:
    conn = get_conn()
    row = conn.execute(q("SELECT COUNT(*) AS n FROM tasks")).fetchone()
    conn.close()
    return row["n"]


# Copied from tests/test_forward_routing.py:214-215 (verbatim).
def _events_of_type(etype: str) -> list:
    return [e for e in event_bus.recent(200) if e.get("type") == etype]


# T-245 helper (adapted from tests/test_forward_routing.py:608-625).
def _declare(admin_token: str, node_id: str, token: str, slots: list) -> None:
    """Declare `slots` via the node heartbeat; heartbeat must stay 200."""
    r = client.post(
        "/relay/v2/discovery/worker-heartbeat",
        json={"forward_slots": slots},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text


# T-245 helper (adapted from tests/test_forward_routing.py:620-625).
def _list_slots(admin_token: str, node_id: str) -> list:
    r = client.get(
        f"/relay/v2/forward-mappings/slots?node={node_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["slots"]


# T-245 helper (adapted from tests/test_forward_routing.py:229-242).
def _submit_forward_task(token: str, slot: str) -> Any:
    """Submit a single forward stage against `slot` via POST /tasks."""
    return client.post(
        BASE + "/tasks",
        json={
            "task_name": "t245",
            "stages": [
                {
                    "stage_name": "s1",
                    "capability": "forward",
                    "payload": {"forward_slot": slot},
                }
            ],
        },
        headers={"Authorization": f"Bearer {token}"},
    )


# ── Migration + metadata (T-245 Task 1 — tests 1-2) ───────────────


def test_migration_16_adds_slot_type_columns():
    """Test 1 — migration 16 adds the 5 typed-slot columns and registers version 16."""
    conn = get_conn()
    cols = {
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(forward_slots)").fetchall()
    }
    versions = {
        r["version"]
        for r in conn.execute(q("SELECT version FROM schema_version")).fetchall()
    }
    assert {"slot_type", "value", "ref_slot", "ref_field", "input_schema"} <= cols, (
        "migration 16 must add slot_type/value/ref_slot/ref_field/input_schema"
    )
    assert 16 in versions, "migration 16 must register itself in schema_version"
    conn.close()


def test_tables_metadata_declares_forward_slots():
    """Test 2 — sa metadata declares forward_slots with column parity to the SQLite table."""
    md_cols = set(metadata.tables["forward_slots"].columns.keys())
    conn = get_conn()
    db_cols = {
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(forward_slots)").fetchall()
    }
    conn.close()
    assert "forward_slots" in metadata.tables
    assert md_cols == db_cols, "metadata declaration must mirror the migrated table"


# ── Declaration roundtrips (Task 2 — tests 3-5, 15) ───────────────


def test_slot_type_text_stored_with_value():
    """Test 3 — a text slot round-trips with slot_type and its native str value."""
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "t245-text", [{"name": "capx", "version": "1.0.0"}]
    )

    _declare(
        admin_token,
        node_id,
        token,
        [{"slot": "1", "label": "L", "type": "text", "value": "probe"}],
    )
    slots = _list_slots(admin_token, node_id)

    assert slots[0]["slot_type"] == "text", "declared type must be returned resolved"
    assert slots[0]["value"] == "probe", "value must be a native str, not a JSON string"
    assert slots[0]["value"] != '"probe"', "value must not leak as a JSON-encoded string"
    assert set(slots[0]) == {
        "node_id", "slot", "label", "description", "slot_type",
        "value", "ref_slot", "ref_field", "input_schema", "updated_at",
    }, "every row carries all 10 response keys (design §5)"


def test_slot_type_bool_stored():
    """Test 4 — bool slots round-trip native True/False values."""
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "t245-bool", [{"name": "capx", "version": "1.0.0"}]
    )

    _declare(
        admin_token,
        node_id,
        token,
        [
            {"slot": "1", "type": "bool", "value": True},
            {"slot": "2", "type": "bool", "value": False},
        ],
    )
    slots = _list_slots(admin_token, node_id)

    assert slots[0]["value"] is True, "bool True must round-trip natively"
    assert slots[1]["value"] is False, "bool False must round-trip natively (not 'false')"
    assert slots[0]["slot_type"] == "bool"
    assert slots[1]["slot_type"] == "bool"


def test_missing_slot_type_defaults_to_forward():
    """Test 5 — a declaration without `type` resolves/persists as forward (D-08)."""
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "t245-legacy", [{"name": "capx", "version": "1.0.0"}]
    )

    _declare(admin_token, node_id, token, [{"slot": "1", "label": "Legacy-Form"}])
    slots = _list_slots(admin_token, node_id)

    assert slots[0]["slot_type"] == "forward", (
        "absent type must surface as the forward default, never NULL"
    )


# ── Declaration rejections (Task 2 — tests 6-14, 15) ──────────────


def test_unknown_slot_type_rejected():
    """Test 6 — unknown type rejected in-unit; heartbeat stays 200 and keeps old rows."""
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "t245-reject-type", [{"name": "capx", "version": "1.0.0"}]
    )

    with pytest.raises(ForwardRoutingError) as exc_info:
        replace_forward_slots(
            node_id, [{"slot": "1", "type": "vector", "value": "x"}]
        )
    exc = exc_info.value
    assert exc.code == "forward_slot_invalid"
    assert exc.http_status == 422
    assert len(_list_slots(admin_token, node_id)) == 0, (
        "a rejected declaration must not touch existing rows"
    )

    # Transport: the heartbeat swallows declaration errors (core/discovery.py
    # :352-358) and the prior declaration stays intact (validation before DELETE).
    _declare(admin_token, node_id, token, [{"slot": "1", "label": "keep"}])
    _declare(admin_token, node_id, token, [{"slot": "9", "type": "vector"}])
    slots = _list_slots(admin_token, node_id)
    assert len(slots) == 1 and slots[0]["slot"] == "1", (
        "an invalid heartbeat must leave exactly the prior declaration row"
    )
    assert slots[0]["label"] == "keep"


def test_bool_value_must_be_real_bool():
    """Test 7 — bool slots reject the string "true" and int 1 (no coercion, C4)."""
    admin_token = _admin()
    node_id, _token = _register_and_approve(
        admin_token, "t245-reject-bool", [{"name": "capx", "version": "1.0.0"}]
    )

    for value in ("true", 1):
        with pytest.raises(ForwardRoutingError) as exc_info:
            replace_forward_slots(
                node_id, [{"slot": "1", "type": "bool", "value": value}]
            )
        exc = exc_info.value
        assert exc.code == "forward_slot_invalid", f"not a real bool: {value!r}"
        assert exc.http_status == 422, f"not a real bool: {value!r}"
    assert len(_list_slots(admin_token, node_id)) == 0, (
        "a rejected declaration must not touch existing rows"
    )


def test_text_value_must_be_string():
    """Test 8 — text slots reject non-string values (42, True)."""
    admin_token = _admin()
    node_id, _token = _register_and_approve(
        admin_token, "t245-reject-text", [{"name": "capx", "version": "1.0.0"}]
    )

    for slot, value in (("1", 42), ("2", True)):
        with pytest.raises(ForwardRoutingError) as exc_info:
            replace_forward_slots(
                node_id, [{"slot": slot, "type": "text", "value": value}]
            )
        exc = exc_info.value
        assert exc.code == "forward_slot_invalid", f"text value must be str: {value!r}"
        assert exc.http_status == 422, f"text value must be str: {value!r}"
    assert len(_list_slots(admin_token, node_id)) == 0, (
        "a rejected declaration must not touch existing rows"
    )


def test_value_on_forward_slot_rejected():
    """Test 9 — value on a forward slot is forbidden (D-02)."""
    admin_token = _admin()
    node_id, _token = _register_and_approve(
        admin_token, "t245-reject-fwdvalue", [{"name": "capx", "version": "1.0.0"}]
    )

    with pytest.raises(ForwardRoutingError) as exc_info:
        replace_forward_slots(node_id, [{"slot": "1", "type": "forward", "value": "x"}])
    exc = exc_info.value
    assert exc.code == "forward_slot_invalid"
    assert exc.http_status == 422
    assert len(_list_slots(admin_token, node_id)) == 0, (
        "a rejected declaration must not touch existing rows"
    )


def test_ref_to_unknown_slot_rejected():
    """Test 10 — a ref pointing at an undeclared slot is rejected (in-memory check)."""
    admin_token = _admin()
    node_id, _token = _register_and_approve(
        admin_token, "t245-reject-refghost", [{"name": "capx", "version": "1.0.0"}]
    )

    with pytest.raises(ForwardRoutingError) as exc_info:
        replace_forward_slots(
            node_id,
            [{"slot": "1", "type": "text", "ref_slot": "nope", "ref_field": "cmd"}],
        )
    exc = exc_info.value
    assert exc.code == "forward_slot_invalid"
    assert exc.http_status == 422
    assert len(_list_slots(admin_token, node_id)) == 0, (
        "a rejected declaration must not touch existing rows"
    )


def test_ref_to_value_slot_rejected():
    """Test 11 — a ref may only target a forward slot (direction value→forward)."""
    admin_token = _admin()
    node_id, _token = _register_and_approve(
        admin_token, "t245-reject-refdir", [{"name": "capx", "version": "1.0.0"}]
    )

    with pytest.raises(ForwardRoutingError) as exc_info:
        replace_forward_slots(
            node_id,
            [
                {"slot": "1", "type": "text"},
                {"slot": "2", "type": "text", "ref_slot": "1", "ref_field": "cmd"},
            ],
        )
    exc = exc_info.value
    assert exc.code == "forward_slot_invalid"
    assert exc.http_status == 422
    assert len(_list_slots(admin_token, node_id)) == 0, (
        "a rejected declaration must not touch existing rows"
    )


def test_ref_on_forward_slot_rejected():
    """Test 12 — refs on a forward slot are rejected."""
    admin_token = _admin()
    node_id, _token = _register_and_approve(
        admin_token, "t245-reject-reffwd", [{"name": "capx", "version": "1.0.0"}]
    )

    with pytest.raises(ForwardRoutingError) as exc_info:
        replace_forward_slots(
            node_id,
            [
                {"slot": "1", "type": "forward", "ref_slot": "2", "ref_field": "f"},
                {"slot": "2", "type": "forward"},
            ],
        )
    exc = exc_info.value
    assert exc.code == "forward_slot_invalid"
    assert exc.http_status == 422
    assert len(_list_slots(admin_token, node_id)) == 0, (
        "a rejected declaration must not touch existing rows"
    )


def test_ref_half_rejected():
    """Test 13 — a half ref (only ref_slot or only ref_field) is rejected."""
    admin_token = _admin()
    node_id, _token = _register_and_approve(
        admin_token, "t245-reject-refhalf", [{"name": "capx", "version": "1.0.0"}]
    )

    for entry in (
        {"slot": "1", "type": "text", "ref_slot": "2"},
        {"slot": "1", "type": "text", "ref_field": "f"},
    ):
        with pytest.raises(ForwardRoutingError) as exc_info:
            replace_forward_slots(node_id, [entry])
        exc = exc_info.value
        assert exc.code == "forward_slot_invalid", f"half ref must be rejected: {entry!r}"
        assert exc.http_status == 422, f"half ref must be rejected: {entry!r}"
    assert len(_list_slots(admin_token, node_id)) == 0, (
        "a rejected declaration must not touch existing rows"
    )


def test_input_schema_on_value_slot_rejected():
    """Test 14 — input_schema on a value slot is rejected (C4)."""
    admin_token = _admin()
    node_id, _token = _register_and_approve(
        admin_token, "t245-reject-ischema", [{"name": "capx", "version": "1.0.0"}]
    )

    with pytest.raises(ForwardRoutingError) as exc_info:
        replace_forward_slots(
            node_id, [{"slot": "1", "type": "text", "input_schema": {"fields": {}}}]
        )
    exc = exc_info.value
    assert exc.code == "forward_slot_invalid"
    assert exc.http_status == 422
    assert len(_list_slots(admin_token, node_id)) == 0, (
        "a rejected declaration must not touch existing rows"
    )


def test_input_schema_forward_slot_roundtrip():
    """Test 15 — a forward slot's input_schema round-trips as a parsed native dict."""
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "t245-schema", [{"name": "capx", "version": "1.0.0"}]
    )

    schema = {"fields": {"command": {"type": "string", "required": True}}}
    _declare(
        admin_token,
        node_id,
        token,
        [{"slot": "1", "type": "forward", "input_schema": schema}],
    )
    slots = _list_slots(admin_token, node_id)

    assert slots[0]["input_schema"] == schema, (
        "input_schema must come back as a parsed dict, not a JSON string"
    )


# ── Submit gate (Task 3 — tests 16-20) ────────────────────────────


def test_submit_against_text_slot_refused():
    """Test 16 — a submit against a declared text slot is refused before mapping (D-04)."""
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "t245-gate-text", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _declare(
        admin_token,
        node_id,
        token,
        [{"slot": "1", "type": "text", "value": "probe"}],
    )
    # Mapping present on purpose: it proves the gate runs BEFORE mapping
    # resolution — without a gate this submit would route (RED fails here).
    _register_capability(admin_token, "submit.misc")
    assert _put_mapping(admin_token, node_id, "1", "submit.misc").status_code == 200

    before = _task_count()
    r = _submit_forward_task(token, "1")

    assert r.status_code == 422, "a declared text slot must not route"
    assert "forward_slot_not_routable" in r.json()["detail"]
    assert _task_count() == before, "a refused gate submit must not create a task"
    assert _events_of_type("forward_refused") == [], (
        "gate refusals are configuration errors — no forward_refused event (D-04)"
    )


def test_submit_against_bool_slot_refused():
    """Test 17 — a submit against a declared bool slot is refused like text."""
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "t245-gate-bool", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _declare(
        admin_token,
        node_id,
        token,
        [{"slot": "1", "type": "bool", "value": False}],
    )
    _register_capability(admin_token, "submit.misc")
    assert _put_mapping(admin_token, node_id, "1", "submit.misc").status_code == 200

    before = _task_count()
    r = _submit_forward_task(token, "1")

    assert r.status_code == 422, "a declared bool slot must not route"
    assert "forward_slot_not_routable" in r.json()["detail"]
    assert _task_count() == before, "a refused gate submit must not create a task"
    assert _events_of_type("forward_refused") == [], (
        "gate refusals are configuration errors — no forward_refused event (D-04)"
    )


def test_submit_against_forward_slot_still_works():
    """Test 18 — regression guard: a declared forward slot keeps routing (design §7)."""
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "t245-fwd", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _declare(admin_token, node_id, token, [{"slot": "1", "type": "forward"}])
    _register_capability(admin_token, "submit.misc")
    assert _put_mapping(admin_token, node_id, "1", "submit.misc").status_code == 200

    r = _submit_forward_task(token, "1")

    assert r.status_code == 200
    # POST /tasks returns a TaskView — the id lives under task.task_id.
    task_id = r.json()["task"]["task_id"]
    conn = get_conn()
    row = conn.execute(
        q(
            "SELECT capability, forward_slot, payload, status FROM task_stages"
            " WHERE task_id = ?",
            (task_id,),
        )
    ).fetchone()
    conn.close()
    assert row["capability"] == "submit.misc", "forward path must keep routing"
    assert row["forward_slot"] == "1", "slot provenance stored on the stage"


def test_submit_undeclared_slot_keeps_forward_mapping_missing():
    """Test 19 — guard: undeclared slots keep the legacy forward_mapping_missing error."""
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "t245-nodecls", [{"name": "submit.misc", "version": "1.0.0"}]
    )

    before = _task_count()
    r = _submit_forward_task(token, "9")

    assert r.status_code == 422
    assert "forward_mapping_missing" in r.json()["detail"], (
        "the gate must only apply to explicitly declared non-forward slots"
    )
    assert _task_count() == before


def test_submit_legacy_null_slot_type_routes():
    """Test 20 — guard: pre-migration NULL slot_type rows keep routing (C2)."""
    admin_token = _admin()
    node_id, token = _register_and_approve(
        admin_token, "t245-legacyrow", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _register_capability(admin_token, "submit.misc")
    assert _put_mapping(admin_token, node_id, "1", "submit.misc").status_code == 200
    # Direct DB insert of a pre-migration row: only the old 5 columns exist
    # pre-T-245 (and slot_type stays NULL on the migrated table).
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO forward_slots (node_id, slot, updated_at)"
            " VALUES (?, '1', '2026-01-01T00:00:00+00:00')",
            (node_id,),
        )
    )
    conn.commit()
    conn.close()

    r = _submit_forward_task(token, "1")

    assert r.status_code == 200, "NULL slot_type must count as the forward default"
    # POST /tasks returns a TaskView — the id lives under task.task_id.
    task_id = r.json()["task"]["task_id"]
    conn = get_conn()
    row = conn.execute(
        q(
            "SELECT capability, forward_slot, payload, status FROM task_stages"
            " WHERE task_id = ?",
            (task_id,),
        )
    ).fetchone()
    conn.close()
    assert row["capability"] == "submit.misc"
    assert row["forward_slot"] == "1", "legacy rows keep slot provenance"