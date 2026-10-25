"""T-247: forward-mapping payload binding — rules + separator (acceptance tests).

Semantics (design ``.hermes/pipeline/t247-binding/design.md`` §2-§8):
``forward_mappings.binding`` carries the Ronny form ``{"rules":
[{"sources": [str, …], "target": str}, …], "separator": "…"}`` (NULL =
legacy passthrough 1:1). The PUT validates the rule FORM loud
(forward_binding_invalid) and the field references against the branch
schema — pin-scoped for pinned mappings, the submitter's slot schema for
unpinned ones (forward_binding_unknown_source_field /
forward_binding_unknown_target_field / forward_binding_requires_schema).
The submit-time rewrite (resolve_forward_stages) projects the payload:
copy (one source) / join (several, separator), unbound non-reserved fields
fly, the RESERVED prompt fields (KNOWN_FLOW_PROMPT_FIELDS — the verbatim
PROMPT_KEYS mirror of iowap-flow/handlers/agent_ai.py:44) survive; the
stored payload stays a JSON string. Binding is payload-only: claim gates,
owner semantics and the event gate are untouched. D-09 (T-245 deferral)
lands as the declaration-side ref_field existence check against the
referenced slot's input_schema (schema-gated refs).

Legacy passthrough (all 6 live mappings, binding NULL) is frozen unchanged
by tests 25/37. Plan: ``.hermes/plans/2026-10-25_t247-binding.md``.
Fixture pattern follows tests/test_forward_routing.py (fresh_db autouse,
TestClient, RELAY_DB_PATH="" env reset).
"""

import json
import os
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
from relay_server.core.forward import (
    ForwardRoutingError,
    validate_binding_form,
)
from relay_server.core.tables import metadata
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


# Copied from tests/test_forward_routing.py:185-195 (verbatim).
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


# Copied from tests/test_t246_forward_pin.py (verbatim; the T-246 _post_task).
def _post_task(token: str, stages: list[dict]) -> Any:
    """Multi-stage POST /relay/v2/scheduler/tasks (test_forward_routing BASE)."""
    return client.post(
        BASE + "/tasks",
        json={"task_name": "t246", "stages": stages},
        headers={"Authorization": f"Bearer {token}"},
    )


# Copied from tests/test_t246_forward_pin.py (verbatim).
def _declare_slot(token: str, slots: list[dict]) -> Any:
    """Worker heartbeat declaring forward slots (tests/test_t249_...:92-97 pattern)."""
    return client.post(
        "/relay/v2/discovery/worker-heartbeat",
        json={"forward_slots": slots},
        headers={"Authorization": f"Bearer {token}"},
    )


# Copied from tests/test_t246_forward_pin.py (verbatim).
def _audit_upsert_rows() -> list:
    """Read forward.mapping_upsert audit rows from the audit_logs table."""
    conn = get_conn()
    rows = conn.execute(
        q("SELECT details FROM audit_logs WHERE action = 'forward.mapping_upsert'")
    ).fetchall()
    conn.close()
    return rows


def _put_binding(admin_token: str, node_id: str, slot: str, target: str,
                 target_node_id: str | None, binding: Any) -> Any:
    """PUT a mapping with explicit pin AND binding forms (T-247).

    Any of the two optionals may be None → the field is OMITTED from the
    body (the full-row-replace probe needs the omission form, design
    D-05). binding never sends ""  — an empty dict form is a model-level
    rejection, not a probe case.
    """
    body: dict = {"target_capability": target}
    if target_node_id is not None:
        body["target_node_id"] = target_node_id
    if binding is not None:
        body["binding"] = binding
    return client.put(
        f"/relay/v2/forward-mappings/{node_id}/{urllib.parse.quote(slot, safe='')}",
        json=body,
        headers={"Authorization": f"Bearer {admin_token}"},
    )


SCHEMA_TASK = {"fields": {"task": {"type": "string", "required": False}}}
R_TASK = {"rules": [{"sources": ["task"], "target": "task"}]}


# ── Migration + schema (T-247 design §8.1 — tests 1-4) ─────────────


def test_migration_18_adds_binding_column():
    """Test 1 — migration 18 adds forward_mappings.binding and registers version 18."""
    conn = get_conn()
    cols = [
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(forward_mappings)").fetchall()
    ]
    versions = {
        r["version"]
        for r in conn.execute(q("SELECT version FROM schema_version")).fetchall()
    }
    conn.close()
    assert "binding" in cols, (
        "migration 18 must add forward_mappings.binding"
    )
    assert 18 in versions, "migration 18 must register itself in schema_version"


def test_tables_metadata_declares_binding_column():
    """Test 2 — sa metadata declares the binding column with PRAGMA parity (T-246 test-2 pattern)."""
    md_maps = set(metadata.tables["forward_mappings"].columns.keys())
    conn = get_conn()
    db_maps = {
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(forward_mappings)").fetchall()
    }
    conn.close()
    assert "binding" in md_maps, (
        "the metadata declaration carries the T-247 binding column"
    )
    assert "binding" in db_maps, (
        "migration 18 must add the column to the live table"
    )
    assert md_maps == db_maps, "metadata declaration must mirror the migrated table"


def test_binding_column_is_nullable():
    """Test 3 — a pre-binding INSERT writes NULL binding (nullable, no backfill)."""
    _seed_node("mnode", capabilities=["agent.ai"])
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO forward_mappings"
            " (mapping_id, node_id, slot, target_capability, target_node_id,"
            " created_at, updated_at)"
            " VALUES ('fwdm_t247', ?, '1', 'agent.ai', NULL,"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            ("mnode",),
        )
    )
    conn.commit()
    row = conn.execute(
        q(
            "SELECT node_id, slot, target_node_id, binding"
            " FROM forward_mappings WHERE mapping_id = 'fwdm_t247'"
        )
    ).fetchone()
    conn.close()
    assert row["node_id"] == "mnode"
    assert row["slot"] == "1"
    assert row["binding"] is None, "no backfill: an unbound row reads NULL"


def test_migration_18_idempotent_on_replay():
    """Test 4 — _m018 called twice on one connection: the column appears exactly once.

    Phase causes (design §8): pre-scaffold the in-body import is an
    ImportError (symbol missing); post-scaffold (pass-stub) the calls run
    clean but the column stays absent → the count assertion fails;
    post-coder GREEN.
    """
    from relay_server.core.db import _m018_forward_binding

    conn = get_conn()
    _m018_forward_binding(conn)
    _m018_forward_binding(conn)
    names = [
        r[1]
        for r in conn.exec_driver_sql("PRAGMA table_info(forward_mappings)").fetchall()
    ]
    conn.close()
    assert names.count("binding") == 1, (
        "replayed migration must leave exactly one forward_mappings.binding"
    )


# ── Form guards — validate_binding_form (design §8.2 — tests 5-9) ──


def test_validate_binding_form_accepts_ronny_forms():
    """Test 5 — a direct call returns the NORMALIZED Ronny form (None passthrough)."""
    assert validate_binding_form(None) is None

    single = validate_binding_form(R_TASK)
    assert single == {
        "rules": [{"sources": ["task"], "target": "task"}],
        "separator": "\n\n",
    }, "the absent separator defaults to DEFAULT_BINDING_SEPARATOR"
    assert isinstance(single["rules"], list)
    assert all(isinstance(rule, dict) for rule in single["rules"])
    assert isinstance(single["separator"], str)

    custom = validate_binding_form(R_TASK | {"separator": "|"})
    assert custom == {
        "rules": [{"sources": ["task"], "target": "task"}],
        "separator": "|",
    }

    empty_sep = validate_binding_form(R_TASK | {"separator": ""})
    assert empty_sep == {
        "rules": [{"sources": ["task"], "target": "task"}],
        "separator": "",
    }, "an empty separator is a legitimate form (adjacent join)"

    multi = validate_binding_form(
        {
            "rules": [
                {"sources": ["a"], "target": "task"},
                {"sources": ["b"], "target": "prompt"},
            ]
        }
    )
    assert [rule["target"] for rule in multi["rules"]] == ["task", "prompt"]
    assert [rule["sources"] for rule in multi["rules"]] == [["a"], ["b"]]


@pytest.mark.parametrize(
    "binding",
    [
        "x",                                          # not a dict (str)
        {"rules": "task"},                            # rules not a list
        {"rules": []},                                # empty rules
        [{"sources": ["task"], "target": "task"}],    # list root
        {"rules": [{"sources": ["task"]}]},           # rule without target
        {"rules": [{"sources": "task", "target": "task"}]},   # sources not a list
        {"rules": [{"sources": [], "target": "task"}]},       # empty sources
        {"rules": [{"sources": [""], "target": "task"}]},     # empty source name
        {"rules": [{"sources": [" x"], "target": "task"}]},   # padded source
        {"rules": [{"sources": ["task"], "target": "x "}]},   # padded target
        {"rules": [{"sources": ["a"], "target": "a"}], "separator": 7},  # separator non-str
        {"rules": [{"sources": ["a" * 256], "target": "task"}]},          # 256-char name
    ],
)
def test_validate_binding_form_shape_rejections(binding):
    """Test 6 — the §3.1 shape-rejection ladder (each → forward_binding_invalid)."""
    with pytest.raises(ForwardRoutingError) as exc_info:
        validate_binding_form(binding)
    assert exc_info.value.code == "forward_binding_invalid"
    assert exc_info.value.http_status == 422


def test_validate_binding_form_disjoint_targets():
    """Test 7 — two rules with the same target → forward_binding_invalid (plan C2 disjunction)."""
    duplicate_targets = {
        "rules": [
            {"sources": ["a"], "target": "x"},
            {"sources": ["b"], "target": "x"},
        ]
    }
    with pytest.raises(ForwardRoutingError) as exc_info:
        validate_binding_form(duplicate_targets)
    assert exc_info.value.code == "forward_binding_invalid"
    assert exc_info.value.http_status == 422


def test_validate_binding_form_allows_chaining_shapes():
    """Test 8 — a source may be another rule's target (order-deterministic, §3.3)."""
    chained = {
        "rules": [
            {"sources": ["task"], "target": "plan"},
            {"sources": ["plan"], "target": "task"},
        ]
    }
    result = validate_binding_form(chained)
    assert result == {
        "rules": [
            {"sources": ["task"], "target": "plan"},
            {"sources": ["plan"], "target": "task"},
        ],
        "separator": "\n\n",
    }


def test_validate_binding_form_duplicate_source_in_one_rule():
    """Test 9 — duplicate sources within ONE rule → forward_binding_invalid."""
    duplicate_source = {"rules": [{"sources": ["a", "a"], "target": "b"}]}
    with pytest.raises(ForwardRoutingError) as exc_info:
        validate_binding_form(duplicate_source)
    assert exc_info.value.code == "forward_binding_invalid"
    assert exc_info.value.http_status == 422


# ── PUT ladder + 422 codes (design §8.3 — tests 10-19) ─────────────


def test_put_binding_roundtrip_row_shape():
    """Test 10 — bound PUT roundtrip: PUT and GET rows carry the binding native dict."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )

    expected_binding = {"rules": [{"sources": ["task"], "target": "task"}],
                        "separator": "\n\n"}
    created = _put_binding(admin_token, submitter_id, "1", "agent.ai", provider_id, R_TASK)
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["binding"] == expected_binding, "PUT response row carries the binding"
    assert set(row) == {
        "node_id", "slot", "target_capability", "target_node_id",
        "binding", "created_at", "updated_at",
    }

    got = _get_mappings(admin_token, node_id=submitter_id)
    assert got.status_code == 200
    rows = got.json()["mappings"]
    assert len(rows) == 1
    assert rows[0]["binding"] == expected_binding, "GET row carries the parsed binding"


def test_put_binding_pinned_unknown_target_field_422():
    """Test 11 — pinned binding target outside the PIN row's own schema → 422.

    Anti-aggregate: Q advertises agent.ai with the target's name — the pin
    row (P, agent.ai) is the universe (design D-03-class; the aggregate
    carries `nope` from Q and must NOT green the check).
    """
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    p_id, _p_token = _register_and_approve(
        admin_token,
        "p",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )
    _q_id, _q_token = _register_and_approve(
        admin_token,
        "q",
        [{"name": "agent.ai", "version": "1.0.0",
          "input_schema": {"fields": {"nope": {"type": "string", "required": False}}}}],
    )

    r = _put_binding(
        admin_token, submitter_id, "1", "agent.ai", p_id,
        {"rules": [{"sources": ["task"], "target": "nope"}]},
    )
    assert r.status_code == 422, r.text
    assert "forward_binding_unknown_target_field" in r.json()["detail"]


def test_put_binding_pin_without_schema_requires_schema_422():
    """Test 12 — pinned binding with a cap row WITHOUT input_schema → 422 requires_schema."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    p_id, _p_token = _register_and_approve(
        admin_token, "p", [{"name": "agent.ai", "version": "1.0.0"}]
    )

    r = _put_binding(admin_token, submitter_id, "1", "agent.ai", p_id, R_TASK)
    assert r.status_code == 422, r.text
    assert "forward_binding_requires_schema" in r.json()["detail"]


def test_put_binding_unpinned_slot_schema_branch_roundtrip():
    """Test 13 — unpinned binding validates against the SLOT schema; roundtrip OK."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _p_id, _p_token = _register_and_approve(
        admin_token,
        "p",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )
    hb = _declare_slot(
        submitter_token,
        [{"slot": "1", "label": "x", "type": "forward", "input_schema": SCHEMA_TASK}],
    )
    assert hb.status_code == 200, hb.text

    expected_binding = {"rules": [{"sources": ["task"], "target": "task"}],
                        "separator": "\n\n"}
    created = _put_binding(admin_token, submitter_id, "1", "agent.ai", None, R_TASK)
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["binding"] == expected_binding, "PUT response row carries the binding"

    got = _get_mappings(admin_token, node_id=submitter_id)
    assert got.status_code == 200
    rows = got.json()["mappings"]
    assert len(rows) == 1
    assert rows[0]["binding"] == expected_binding, "GET row carries the parsed binding"


def test_put_binding_unpinned_requires_schema_422():
    """Test 14 — unpinned binding with a slot that declares no input_schema → 422."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _p_id, _p_token = _register_and_approve(
        admin_token, "p", [{"name": "agent.ai", "version": "1.0.0"}]
    )
    hb = _declare_slot(submitter_token, [{"slot": "1", "label": "x"}])
    assert hb.status_code == 200, hb.text

    r = _put_binding(admin_token, submitter_id, "1", "agent.ai", None, R_TASK)
    assert r.status_code == 422, r.text
    assert "forward_binding_requires_schema" in r.json()["detail"]


def test_put_binding_unknown_source_field_422():
    """Test 15 — binding source outside the declared slot schema → 422 unknown_source_field."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _p_id, _p_token = _register_and_approve(
        admin_token,
        "p",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )
    hb = _declare_slot(
        submitter_token,
        [{"slot": "1", "label": "x", "type": "forward", "input_schema": SCHEMA_TASK}],
    )
    assert hb.status_code == 200, hb.text

    r = _put_binding(
        admin_token, submitter_id, "1", "agent.ai", None,
        {"rules": [{"sources": ["nosuch"], "target": "task"}]},
    )
    assert r.status_code == 422, r.text
    assert "forward_binding_unknown_source_field" in r.json()["detail"]


def test_put_binding_model_vs_ladder_details():
    """Test 16 — model forms are list-detail 422s; the ladder form is a string 422."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    p_id, _p_token = _register_and_approve(
        admin_token,
        "p",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )

    r_int = _put_binding(admin_token, submitter_id, "1", "agent.ai", p_id, 1)
    assert r_int.status_code == 422, r_int.text
    assert isinstance(r_int.json()["detail"], list), (
        "a model form-guard 422 must be a list, not a ladder string detail"
    )

    r_rules = _put_binding(admin_token, submitter_id, "1", "agent.ai", p_id, {"rules": []})
    assert r_rules.status_code == 422, r_rules.text
    assert isinstance(r_rules.json()["detail"], list), (
        "a model form-guard 422 must be a list, not a ladder string detail"
    )

    # Model-level duplicate targets PASS the transport shape; the LADDER
    # refuses with a string detail (the two detail shapes are distinct).
    r_dup = _put_binding(
        admin_token, submitter_id, "1", "agent.ai", p_id,
        {"rules": [
            {"sources": ["task"], "target": "task"},
            {"sources": ["prompt"], "target": "task"},
        ]},
    )
    assert r_dup.status_code == 422, r_dup.text
    assert isinstance(r_dup.json()["detail"], str), (
        "a ladder 422 must be a string detail, not a model list detail"
    )
    assert "forward_binding_invalid" in r_dup.json()["detail"]


def test_put_bad_pin_outranks_bad_binding():
    """Test 17 (GUARD) — a PUT carrying BOTH a bad pin and a bad binding fails on the PIN code first."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    p_id, _p_token = _register_and_approve(
        admin_token,
        "p",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )
    duplicate_targets = {
        "rules": [
            {"sources": ["task"], "target": "task"},
            {"sources": ["prompt"], "target": "task"},
        ]
    }

    bad = _put_binding(admin_token, submitter_id, "1", "agent.ai", "NOPE1234", duplicate_targets)
    assert bad.status_code == 422, bad.text
    assert bad.json()["detail"].startswith("target_node_unknown:"), (
        "the PIN failure outranks the binding failure (steps 5-6 precede 8-9)"
    )

    # After fixing the pin the binding error can surface (CO); binding is
    # inert at SC (200) and the pin-priority guard above still holds — the
    # binding error never masquerades as a pin code (order, not codes).
    fixed = _put_binding(admin_token, submitter_id, "1", "agent.ai", p_id, duplicate_targets)
    if fixed.status_code != 200:
        assert "forward_binding_invalid" in fixed.json()["detail"], (
            "after fixing the pin the binding error surfaces"
        )
        assert not fixed.json()["detail"].startswith("target_node_unknown"), (
            "the binding error never masquerades as the pin error"
        )


def test_put_binding_full_row_replace():
    """Test 18 — a PUT omitting binding replaces the stored binding with NULL (D-05)."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )

    expected_binding = {"rules": [{"sources": ["task"], "target": "task"}],
                        "separator": "\n\n"}
    bound = _put_binding(admin_token, submitter_id, "1", "agent.ai", provider_id, R_TASK)
    assert bound.status_code == 200, bound.text
    rows = _get_mappings(admin_token, node_id=submitter_id).json()["mappings"]
    assert rows[0]["binding"] == expected_binding, (
        "arrangement precondition: the binding was stored before the unbind PUT"
    )

    unbound = _put_binding(admin_token, submitter_id, "1", "agent.ai", provider_id, None)
    assert unbound.status_code == 200, unbound.text
    rows = _get_mappings(admin_token, node_id=submitter_id).json()["mappings"]
    assert len(rows) == 1
    assert rows[0]["binding"] is None, "omitting binding must unbind (full-row replace)"


def test_put_audit_detail_carries_binding_state():
    """Test 19 — the forward.mapping_upsert audit details carry binding=on / binding=legacy."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )

    bound = _put_binding(admin_token, submitter_id, "1", "agent.ai", provider_id, R_TASK)
    assert bound.status_code == 200, bound.text
    details = [r["details"] for r in _audit_upsert_rows()]
    assert any("binding=on" in d for d in details), (
        "the bound PUT detail must carry binding=on"
    )

    legacy = _put_binding(admin_token, submitter_id, "1", "agent.ai", provider_id, None)
    assert legacy.status_code == 200, legacy.text
    details = [r["details"] for r in _audit_upsert_rows()]
    assert any("binding=legacy" in d for d in details), (
        "the unbound PUT detail must carry binding=legacy"
    )


# ── Submit rewrite (design §8.4 — tests 20-28) ─────────────────────


def test_binding_copy_unbound_flies():
    """Test 20 — a copy rule copies verbatim; unbound non-reserved fields fly."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )
    assert _put_binding(
        admin_token, submitter_id, "1", "agent.ai", provider_id, R_TASK
    ).status_code == 200

    r = _forward_submit(submitter_token, {"forward_slot": "1", "task": "T", "drift": "D"})
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    assert json.loads(_stage_row(task_id)["payload"]) == {"task": "T"}, (
        "copy projects task; unbound non-reserved drift flies"
    )


def test_binding_join_default_and_custom_separator():
    """Test 21 — join rules with the default AND a custom separator (non-str json.dumps'd)."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0",
          "input_schema": {"fields": {
              "task": {"type": "string", "required": False},
              "prompt": {"type": "string", "required": False},
          }}}],
    )
    assert _put_binding(
        admin_token, submitter_id, "1", "agent.ai", provider_id,
        {"rules": [{"sources": ["systemprompt", "taskprompt"], "target": "prompt"}]},
    ).status_code == 200
    assert _put_binding(
        admin_token, submitter_id, "2", "agent.ai", provider_id,
        {"rules": [{"sources": ["a", "b"], "target": "task"}], "separator": " | "},
    ).status_code == 200

    r1 = _forward_submit(
        submitter_token, {"forward_slot": "1", "systemprompt": "A", "taskprompt": "B"}
    )
    assert r1.status_code == 200, r1.text
    assert json.loads(_stage_row(r1.json()["task_id"])["payload"]) == {"prompt": "A\n\nB"}, (
        "join with the default separator"
    )

    r2 = _forward_submit(
        submitter_token, {"forward_slot": "2", "a": "A", "b": {"k": 1}}
    )
    assert r2.status_code == 200, r2.text
    assert json.loads(_stage_row(r2.json()["task_id"])["payload"]) == {"task": "A | {\"k\": 1}"}, (
        "join with the custom separator; non-str parts json.dumps'd"
    )


def test_binding_reserved_prompt_field_survives():
    """Test 22 — fired rules consume their sources; the RESERVED prompt field survives."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0",
          "input_schema": {"fields": {
              "task": {"type": "string", "required": False},
              "notes": {"type": "string", "required": False},
              "prompt": {"type": "string", "required": False},
          }}}],
    )
    # (a) x → notes: the fired rule consumes x; the unbound RESERVED task survives.
    assert _put_binding(
        admin_token, submitter_id, "1", "agent.ai", provider_id,
        {"rules": [{"sources": ["x"], "target": "notes"}]},
    ).status_code == 200
    # (b) task → prompt: a BOUND reserved field does not double-survive.
    assert _put_binding(
        admin_token, submitter_id, "2", "agent.ai", provider_id,
        {"rules": [{"sources": ["task"], "target": "prompt"}]},
    ).status_code == 200

    r1 = _forward_submit(submitter_token, {"forward_slot": "1", "task": "T", "x": "1"})
    assert r1.status_code == 200, r1.text
    assert json.loads(_stage_row(r1.json()["task_id"])["payload"]) == {"task": "T", "notes": "1"}, (
        "the fired rule consumed x; the unbound RESERVED task survived"
    )

    r2 = _forward_submit(submitter_token, {"forward_slot": "2", "task": "T"})
    assert r2.status_code == 200, r2.text
    assert json.loads(_stage_row(r2.json()["task_id"])["payload"]) == {"prompt": "T"}, (
        "a bound reserved field does NOT double-survive"
    )


def test_binding_dormant_rules_still_project():
    """Test 23 — zero fired rules still project: unbound drift flies (kein stillem Passthrough)."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0",
          "input_schema": {"fields": {
              "task": {"type": "string", "required": False},
              "o": {"type": "string", "required": False},
          }}}],
    )
    assert _put_binding(
        admin_token, submitter_id, "1", "agent.ai", provider_id,
        {"rules": [{"sources": ["absent"], "target": "o"}]},
    ).status_code == 200

    r = _forward_submit(submitter_token, {"forward_slot": "1", "task": "T", "drift": "D"})
    assert r.status_code == 200, r.text
    assert json.loads(_stage_row(r.json()["task_id"])["payload"]) == {"task": "T"}, (
        "all rules dormant: no target written; unbound drift flies"
    )


def test_binding_corrupt_db_row_submits_422_no_event():
    """Test 24 — a corrupt stored binding refuses the submit LOUD (no event, no task)."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )
    assert _put_binding(
        admin_token, submitter_id, "1", "agent.ai", provider_id, R_TASK
    ).status_code == 200

    # Direct-DB corruption of the stored binding — the submit-time re-parse
    # is the form boundary (design §6; the get_conn/q direct-DB idiom).
    conn = get_conn()
    conn.execute(
        q(
            "UPDATE forward_mappings SET binding = '{not json'"
            " WHERE node_id = ? AND slot = '1'",
            (submitter_id,),
        )
    )
    conn.commit()
    conn.close()

    before = _task_count()
    r = _forward_submit(submitter_token, {"forward_slot": "1", "task": "T"})
    assert r.status_code == 422, r.text
    assert "forward_binding_invalid" in r.json()["detail"], (
        "corrupt stored binding refuses the submit loudly"
    )
    assert _task_count() == before, "the refused submit must not create any task"
    assert _events_of_type("forward_refused") == [], (
        "forward_binding_invalid is a naked raise — no event (event gate untouched)"
    )


def test_binding_null_is_legacy_passthrough():
    """Test 25 (GUARD) — binding NULL: byte-identical legacy 1:1 passthrough."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _register_capability(admin_token, "agent.ai")
    assert _put_mapping(admin_token, submitter_id, "1", "agent.ai").status_code == 200

    r = _forward_submit(submitter_token, {"forward_slot": "1", "task": "T", "drift": "D"})
    assert r.status_code == 200, r.text
    assert json.loads(_stage_row(r.json()["task_id"])["payload"]) == {"task": "T", "drift": "D"}, (
        "legacy passthrough: only forward_slot is stripped"
    )


def test_binding_multi_stage_two_bindings_two_targets():
    """Test 26 — two bound forward stages in ONE task; bindings are stage-local."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    p1_id, _p1_token = _register_and_approve(
        admin_token,
        "p1",
        [{"name": "cap.a", "version": "1.0.0",
          "input_schema": {"fields": {"task": {"type": "string", "required": False}}}}],
    )
    p2_id, _p2_token = _register_and_approve(
        admin_token,
        "p2",
        [{"name": "cap.b", "version": "1.0.0",
          "input_schema": {"fields": {"task": {"type": "string", "required": False}}}}],
    )
    assert _put_binding(
        admin_token, submitter_id, "1", "cap.a", p1_id, R_TASK
    ).status_code == 200
    assert _put_binding(
        admin_token, submitter_id, "2", "cap.b", p2_id,
        {"rules": [{"sources": ["systemprompt", "taskprompt"], "target": "task"}],
         "separator": " | "},
    ).status_code == 200

    r = _post_task(
        submitter_token,
        [
            {"stage_name": "s1", "capability": "forward",
             "payload": {"forward_slot": "1", "task": "T1", "drift": "D"}},
            {"stage_name": "s2", "capability": "forward",
             "payload": {"forward_slot": "2", "systemprompt": "A", "taskprompt": "B"}},
        ],
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task"]["task_id"]

    conn = get_conn()
    rows = conn.execute(
        q(
            "SELECT stage_name, payload FROM task_stages WHERE task_id = ?"
            " ORDER BY sequence ASC",
            (task_id,),
        )
    ).fetchall()
    conn.close()
    by_name = {row["stage_name"]: row for row in rows}
    assert set(by_name) == {"s1", "s2"}
    assert json.loads(by_name["s1"]["payload"]) == {"task": "T1"}, "stage 1 copy projects"
    assert json.loads(by_name["s2"]["payload"]) == {"task": "A | B"}, "stage 2 join projects"
    task = _task_row(task_id)
    assert task["owner_node_id"] is None, "owner is still unset before the claim"


def test_slot_type_gate_precedes_binding():
    """Test 27 (GUARD) — the T-245 type gate precedes any binding usage of the slot."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, _provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )
    hb = _declare_slot(
        submitter_token, [{"slot": "1", "label": "x", "type": "text", "value": "v"}]
    )
    assert hb.status_code == 200, hb.text

    # The bound PUT succeeds anyway (PUT does not consult forward_slots,
    # T-246 D-16 stands); the submit refuses BEFORE mapping/binding
    # resolution.
    put = _put_binding(admin_token, submitter_id, "1", "agent.ai", provider_id, R_TASK)
    assert put.status_code == 200, put.text

    r = _forward_submit(submitter_token, {"forward_slot": "1", "task": "T"})
    assert r.status_code == 422, r.text
    assert "forward_slot_not_routable" in r.json()["detail"], (
        "the type gate precedes mapping/binding resolution"
    )


def test_binding_claim_and_complete_chain_intact():
    """Test 28 (GUARD) — a BOUND mapping does not disturb the submit→claim→complete chain."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    provider_id, provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )
    assert _put_binding(
        admin_token, submitter_id, "1", "agent.ai", provider_id, R_TASK
    ).status_code == 200

    # The payload is an identity fixture: projected form == passthrough form
    # ({"task": "T"} — copy of task onto task, nothing unbound), so the
    # asserted payload form holds in every phase.
    r = _forward_submit(submitter_token, {"forward_slot": "1", "task": "T"})
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    claim = client.post(
        BASE + "/claim",
        json={"capability": "agent.ai"},
        headers={"Authorization": f"Bearer {provider_token}"},
    )
    assert claim.status_code == 200 and claim.json()["claimed"] is True
    assert claim.json()["stage"]["payload"] == {"task": "T"}, (
        "the claiming node sees the REWRITTEN payload (projected form)"
    )
    assert _task_row(task_id)["owner_node_id"] == provider_id, (
        "the claim flips the owner (claim/owner semantics untouched)"
    )

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
    assert task["status"] == "completed", "the bound chain completes the task"


# ── API/entry/models (design §8.5 — tests 29-32) ───────────────────


def test_forward_mapping_put_model_binding_forms():
    """Test 29 — ForwardMappingPut form-guards binding (int/empty-rules → ValidationError)."""
    from relay_server.models import ForwardMappingPut

    default = ForwardMappingPut(target_capability="agent.ai")
    assert default.binding is None, "the binding default is None (legacy passthrough)"

    valid = ForwardMappingPut(
        target_capability="agent.ai",
        binding={"rules": [{"sources": ["task"], "target": "task"}]},
    )
    assert valid.binding is not None
    assert valid.binding.rules[0].target == "task"
    assert valid.binding.rules[0].sources == ["task"]
    assert valid.binding.separator == "\n\n", (
        "the default separator is the two-newline default"
    )

    with pytest.raises(pydantic.ValidationError):
        ForwardMappingPut(target_capability="agent.ai", binding={"rules": []})
    with pytest.raises(pydantic.ValidationError):
        ForwardMappingPut(target_capability="agent.ai", binding=1)


def test_forward_mapping_entry_model_carries_binding():
    """Test 30 — ForwardMappingEntry round-trips the binding; default None; 7-key dump set."""
    from relay_server.models import ForwardMappingEntry

    unpinned = ForwardMappingEntry(
        node_id="S",
        slot="1",
        target_capability="agent.ai",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    assert unpinned.binding is None, "the binding default is None (legacy passthrough)"

    bound = ForwardMappingEntry(
        node_id="S",
        slot="1",
        target_capability="agent.ai",
        target_node_id="P",
        binding={"rules": [{"sources": ["task"], "target": "task"}], "separator": "\n\n"},
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    assert bound.binding == {
        "rules": [{"sources": ["task"], "target": "task"}], "separator": "\n\n"
    }, "the binding round-trips"
    dumped = bound.model_dump()
    assert dumped["binding"] == {
        "rules": [{"sources": ["task"], "target": "task"}], "separator": "\n\n"
    }, "the binding round-trips through model_dump"
    assert set(dumped) == {
        "node_id", "slot", "target_capability", "target_node_id",
        "binding", "created_at", "updated_at",
    }


def test_get_and_unbound_put_rows_carry_binding_key():
    """Test 31 — even unbound PUT/GET rows carry binding (value None) — the 7-key set."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    _register_capability(admin_token, "agent.ai")
    assert _put_mapping(admin_token, submitter_id, "1", "agent.ai").status_code == 200

    for resp in (_get_mappings(admin_token, node_id=submitter_id), _get_mappings(admin_token)):
        assert resp.status_code == 200
        rows = [m for m in resp.json()["mappings"] if m["node_id"] == submitter_id]
        assert len(rows) == 1
        for row in rows:
            assert "binding" in row, "rows carry the binding key (value None)"
            assert row["binding"] is None, "an unbound row reads None"
            assert set(row) == {
                "node_id", "slot", "target_capability", "target_node_id",
                "binding", "created_at", "updated_at",
            }


def test_binding_rules_model_dumps_to_canonical_form():
    """Test 32 — BindingRules.model_dump() is the exact canonical storage shape."""
    from relay_server.models import BindingRules

    model = BindingRules(
        rules=[
            {"sources": ["task"], "target": "task"},
            {"sources": ["systemprompt", "taskprompt"], "target": "prompt"},
        ],
        separator=" | ",
    )
    assert model.model_dump() == {
        "rules": [
            {"sources": ["task"], "target": "task"},
            {"sources": ["systemprompt", "taskprompt"], "target": "prompt"},
        ],
        "separator": " | ",
    }, "the exact storage shape the ladder writes"


# ── D-09 — declaration-side ref_field checks (design §8.6 — tests 33-35) ──


def test_ref_field_must_exist_in_referenced_schema_rollback():
    """Test 33 — an invalid ref_field re-declaration is rejected; the previous rows stand."""
    admin_token = _admin()
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    valid_slots = [
        {"slot": "fwd", "type": "forward", "input_schema": SCHEMA_TASK},
        {"slot": "v", "type": "text", "value": "d", "ref_slot": "fwd", "ref_field": "task"},
    ]
    hb = _declare_slot(submitter_token, valid_slots)
    assert hb.status_code == 200, hb.text
    listed = client.get(
        f"/relay/v2/forward-mappings/slots?node={submitter_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert listed.status_code == 200, listed.text
    by_slot = {r["slot"]: r for r in listed.json()["slots"]}
    assert by_slot["fwd"]["input_schema"] == SCHEMA_TASK
    assert by_slot["v"]["ref_field"] == "task"

    # The invalid re-declaration is swallowed by the heartbeat (200) and
    # must NOT replace the rows (schema-gated ref check + two-phase
    # rollback).
    bad = _declare_slot(
        submitter_token,
        [
            {"slot": "fwd", "type": "forward", "input_schema": SCHEMA_TASK},
            {"slot": "v", "type": "text", "value": "d",
             "ref_slot": "fwd", "ref_field": "nosuch"},
        ],
    )
    assert bad.status_code == 200, bad.text
    listed = client.get(
        f"/relay/v2/forward-mappings/slots?node={submitter_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert listed.status_code == 200, listed.text
    by_slot = {r["slot"]: r for r in listed.json()["slots"]}
    assert by_slot["v"]["ref_field"] == "task", (
        "the invalid declaration never replaced the previous valid rows"
    )
    assert by_slot["fwd"]["input_schema"] == SCHEMA_TASK, "the valid fwd row stands"


def test_ref_field_unknown_direct_call_raises_slot_invalid():
    """Test 34 — a declared ref_field outside the referenced slot's schema → forward_slot_invalid."""
    from relay_server.core.forward import replace_forward_slots

    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )

    with pytest.raises(ForwardRoutingError) as exc_info:
        replace_forward_slots(
            submitter_id,
            [
                {"slot": "fwd", "type": "forward", "input_schema": SCHEMA_TASK},
                {"slot": "v", "type": "text", "value": "d",
                 "ref_slot": "fwd", "ref_field": "nosuch"},
            ],
        )
    assert exc_info.value.code == "forward_slot_invalid"
    assert "ref_field 'nosuch'" in exc_info.value.message


def test_ref_to_slot_without_schema_and_unparseable_schema_422():
    """Test 35 — schema-gated refs: no input_schema and a scalar spec both → forward_slot_invalid."""
    from relay_server.core.forward import replace_forward_slots

    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )

    # (a) the referenced forward slot declares NO input_schema → schema-gated.
    with pytest.raises(ForwardRoutingError) as exc_a:
        replace_forward_slots(
            submitter_id,
            [
                {"slot": "fwd", "type": "forward"},
                {"slot": "v", "type": "text", "value": "d",
                 "ref_slot": "fwd", "ref_field": "task"},
            ],
        )
    assert exc_a.value.code == "forward_slot_invalid"

    # (b) the referenced schema carries a scalar spec — the parse guard
    # wraps from_dict's crash class (never a bare AttributeError/500).
    with pytest.raises(ForwardRoutingError) as exc_b:
        replace_forward_slots(
            submitter_id,
            [
                {"slot": "fwd", "type": "forward",
                 "input_schema": {"fields": {"task": "string"}}},
                {"slot": "v", "type": "text", "value": "d",
                 "ref_slot": "fwd", "ref_field": "task"},
            ],
        )
    assert exc_b.value.code == "forward_slot_invalid"
    assert "referenced slot 'fwd'" in exc_b.value.message


# ── Node-delete/unpin survival (design §8.7 — tests 36-37) ─────────


def test_node_delete_unpin_keeps_binding():
    """Test 36 — deleting the pin target unpins but keeps the binding (C7: no auto-unbind)."""
    admin_token = _admin()
    submitter_id, _submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    x_id, _x_token = _register_and_approve(
        admin_token,
        "x",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )
    assert _put_binding(
        admin_token, submitter_id, "1", "agent.ai", x_id, R_TASK
    ).status_code == 200

    delete = client.delete(
        f"/relay/v2/admin/nodes/{x_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert delete.status_code == 200, delete.text

    conn = get_conn()
    row = conn.execute(
        q(
            "SELECT node_id, slot, target_capability, target_node_id, binding"
            " FROM forward_mappings WHERE node_id = ?",
            (submitter_id,),
        )
    ).fetchone()
    conn.close()
    assert row is not None, "the mapping row survives the pin target's deletion"
    assert row["node_id"] == submitter_id
    assert row["slot"] == "1"
    assert row["target_node_id"] is None, "the pin reference is voided (T-246 C5)"
    assert json.loads(row["binding"]) == {
        "rules": [{"sources": ["task"], "target": "task"}], "separator": "\n\n"
    }, "no auto-unbind: the binding survives untouched (C7)"


def test_node_delete_still_drops_submitter_mappings_with_binding():
    """Test 37 (GUARD) — the deleted node's OWN bound mapping dies with the row (legacy)."""
    admin_token = _admin()
    x_id, x_token = _register_and_approve(
        admin_token,
        "x",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TASK}],
    )
    hb = _declare_slot(
        x_token, [{"slot": "1", "label": "x", "type": "forward", "input_schema": SCHEMA_TASK}]
    )
    assert hb.status_code == 200, hb.text
    assert _put_binding(
        admin_token, x_id, "1", "agent.ai", None, R_TASK
    ).status_code == 200

    delete = client.delete(
        f"/relay/v2/admin/nodes/{x_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert delete.status_code == 200, delete.text

    conn = get_conn()
    n = conn.execute(
        q("SELECT COUNT(*) AS n FROM forward_mappings WHERE node_id = ?", (x_id,))
    ).fetchone()["n"]
    conn.close()
    assert n == 0, "the deleted node's own bound mapping is GONE (legacy behavior)"