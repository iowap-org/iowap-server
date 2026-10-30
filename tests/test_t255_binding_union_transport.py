"""T-255: binding-rules transport model — form-strikt Union + Const-Wert-Akzeptanz.

Audit 2026-10-30 H1: ``BindingRule`` had only ``sources``/``target`` (pydantic
``extra=ignore``), so two classes of silent breakage existed at the
``PUT /relay/v2/forward-mappings`` transport layer:

* a const rule ``{"target": …, "value": …}`` (T-252 form, offered by the
  T-254-r2 dashboard editor, static/node-profile.js) failed the transport
  model with a bogus ``sources``-missing 422 BEFORE the real ladder could
  run — the Ronny T-254-r2 editor form was server-side dead; and
* a MIXED rule ``{"sources": …, "target": …, "value": "y"}`` parsed as a
  copy rule whose ``value`` was silently dropped (extra=ignore) and stored
  as an innocent copy binding — silent data loss.

Fix (this task): the transport carries a union of EXACT rule shapes —
``CopyRule{sources, target}`` | ``ConstRule{target, value}``, both
``extra=forbid`` — so a mixed rule is a clean 422 at the transport layer
instead of a silent loss, and const rules reach the ladder.

Semantics decisions carried by this file:

* **M1 (const value types):** a const ``value`` may be every JSON value —
  ``str`` / ``bool`` / ``int`` / ``float`` AND ``list`` / ``dict`` (plus
  the explicit-null write from T-252). ``validate_binding_form`` (core)
  and the transport model now accept them UNIFORMLY — check_binding_schema
``:552-553`` already declared ``array``/``object`` valid types; the
  T-254-r2 editor offers an object-typed const textarea, so a rejection in
  the core form was a dead editor branch. Non-JSON instances (the ladder
  is also called directly) stay rejected.
* **M7 (caps):** ``rules`` is capped at 64 entries (min 1) and
  ``separator`` at 64 chars — the editor cannot bloat a payload row.

The PUT ladders re-validate the form through
``relay_server.core.forward.validate_binding_form`` afterwards — single
semantic authority, unchanged. Fixture pattern follows
tests/test_t247_forward_binding.py (fresh_db autouse, TestClient,
RELAY_DB_PATH="" env reset).
"""

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
    apply_binding,
    validate_binding_form,
)
from relay_server.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test and reset the event-bus history."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t255.db"
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


# Copied from tests/test_t247_forward_binding.py:74-101 (verbatim).
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
            "node_name": "Admin T255",
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


def _declare_slot(token: str, slots: list[dict]) -> Any:
    """Worker heartbeat declaring forward slots (tests/test_t249_... pattern)."""
    return client.post(
        "/relay/v2/discovery/worker-heartbeat",
        json={"forward_slots": slots},
        headers={"Authorization": f"Bearer {token}"},
    )


def _put_mapping(admin_token: str, node_id: str, slot: str, target: str,
                 target_node_id: str, binding: dict) -> Any:
    return client.put(
        f"/relay/v2/forward-mappings/{node_id}/{urllib.parse.quote(slot, safe='')}",
        json={"target_capability": target, "target_node_id": target_node_id,
              "binding": binding},
        headers={"Authorization": f"Bearer {admin_token}"},
    )


def _get_mappings(admin_token: str, node_id: str | None = None) -> Any:
    url = "/relay/v2/forward-mappings"
    if node_id is not None:
        url += f"?node={node_id}"
    return client.get(url, headers={"Authorization": f"Bearer {admin_token}"})


SCHEMA_TARGET = {
    "fields": {
        "task": {"type": "string", "required": False},
        "model": {"type": "string", "required": False},
        "think": {"type": "boolean", "required": False},
        "cfg": {"type": "object", "required": False},
        "items": {"type": "array", "required": False},
        "free": {"required": False},  # name-only spec (no declared type)
    }
}
SCHEMA_SLOT_ONLY_TASK = {
    "fields": {"task": {"type": "string", "required": False}}
}


def _stage_forward_world(admin_token: str) -> tuple[str, str]:
    """Submitter (slot declared with input_schema) + pinned provider, both live."""
    submitter_id, submitter_token = _register_and_approve(
        admin_token, "submitter", [{"name": "submit.misc", "version": "1.0.0"}]
    )
    hb = _declare_slot(
        submitter_token,
        [{"slot": "1", "label": "x", "type": "forward",
          "input_schema": SCHEMA_SLOT_ONLY_TASK}],
    )
    assert hb.status_code == 200, hb.text
    provider_id, _provider_token = _register_and_approve(
        admin_token,
        "provider",
        [{"name": "agent.ai", "version": "1.0.0", "input_schema": SCHEMA_TARGET}],
    )
    return submitter_id, provider_id


# ── HTTP-PUT acceptance (the T-PFLICHT: TestClient against the router) ──


def test_put_const_rule_saves_and_roundtrips():
    """Const rules reach the ladder and store their value verbatim.

    RED (Audit H1): the transport model rejected the T-252 const form
    (``sources`` was a required field) → 422 ``missing`` before any
    semantic check. GREEN: the Ronny editor form persists.
    """
    admin_token = _admin()
    submitter_id, provider_id = _stage_forward_world(admin_token)
    binding = {
        "rules": [
            {"sources": ["task"], "target": "task"},
            {"target": "think", "value": False},
            {"target": "model", "value": "qwen3.5:2b"},
        ],
        "separator": "\n\n",
    }
    r = _put_mapping(admin_token, submitter_id, "1", "agent.ai", provider_id, binding)
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["binding"] == binding, "the const values survive the PUT verbatim"

    got = _get_mappings(admin_token, node_id=submitter_id)
    assert got.status_code == 200
    rows = got.json()["mappings"]
    assert len(rows) == 1
    assert rows[0]["binding"] == binding, "GET carries the const binding"


def test_put_object_and_array_const_saved():
    """M1: object/array const values pass the transport AND the ladder.

    RED: the core form rejected non-primitive consts and the transport
    dropped/rejected them — now they store natively (T-254-r2 editor
    object-const textarea branch).
    """
    admin_token = _admin()
    submitter_id, provider_id = _stage_forward_world(admin_token)
    obj_const = {"mode": "fast", "retries": 2}
    list_const = ["a", "b", 3]
    binding = {
        "rules": [
            {"target": "cfg", "value": obj_const},
            {"target": "items", "value": list_const},
        ],
    }
    r = _put_mapping(admin_token, submitter_id, "1", "agent.ai", provider_id, binding)
    assert r.status_code == 200, r.text
    expected = {"rules": binding["rules"], "separator": "\n\n"}
    assert r.json()["binding"] == expected

    stored = _get_mappings(admin_token, node_id=submitter_id).json()["mappings"][0]
    assert stored["binding"]["rules"][0]["value"] == obj_const
    assert stored["binding"]["rules"][1]["value"] == list_const


def test_put_const_null_write_roundtrips():
    """T-252 explicit-null form still round-trips through the new transport.

    The target field declares NO type (name-only, D-06) — an explicit null
    against a typed field stays a 422 (T-252 contract, check_binding_schema).
    """
    admin_token = _admin()
    submitter_id, provider_id = _stage_forward_world(admin_token)
    binding = {"rules": [{"target": "free", "value": None}]}
    r = _put_mapping(admin_token, submitter_id, "1", "agent.ai", provider_id, binding)
    assert r.status_code == 200, r.text
    assert r.json()["binding"] == {"rules": [{"target": "free", "value": None}],
                                   "separator": "\n\n"}


def test_put_mixed_rule_is_422_not_silent_copy_loss():
    """A rule with sources AND value is a clean 422 at the transport layer.

    RED (the silent-loss repro, Audit H1): pydantic ``extra=ignore``
    dropped the ``value`` key, the rule parsed as a COPY rule and the PUT
    stored it — the mixed rule must now fail the transport with 422.
    """
    admin_token = _admin()
    submitter_id, provider_id = _stage_forward_world(admin_token)
    binding = {
        "rules": [
            {"sources": ["task"], "target": "task", "value": "sneaky"},
        ],
    }
    r = _put_mapping(admin_token, submitter_id, "1", "agent.ai", provider_id, binding)
    assert r.status_code == 422, r.text
    # Nothing was written — the mapping must not exist in any lossy form.
    got = _get_mappings(admin_token, node_id=submitter_id)
    assert got.json()["mappings"] == []


def test_put_separator_over_64_chars_is_422():
    """M7: the separator cap binds at the transport layer (64 = boundary)."""
    admin_token = _admin()
    submitter_id, provider_id = _stage_forward_world(admin_token)
    binding = {
        "rules": [{"sources": ["task"], "target": "task"}],
        "separator": "s" * 64,
    }
    ok = _put_mapping(admin_token, submitter_id, "1", "agent.ai", provider_id, binding)
    assert ok.status_code == 200, ok.text
    too_long = {
        "rules": [{"sources": ["task"], "target": "task"}],
        "separator": "s" * 65,
    }
    r = _put_mapping(admin_token, submitter_id, "1", "agent.ai", provider_id, too_long)
    assert r.status_code == 422, r.text


# ── Transport model (union) ─────────────────────────────────────────


def _rules_model():
    from relay_server.models import BindingRules

    return BindingRules


def test_binding_rules_union_parses_copy_and_const_forms():
    BindingRules = _rules_model()
    from relay_server.models import ConstRule, CopyRule

    model = BindingRules(
        rules=[
            {"sources": ["task"], "target": "task"},
            {"target": "think", "value": False},
        ]
    )
    assert isinstance(model.rules[0], CopyRule)
    assert isinstance(model.rules[1], ConstRule)
    assert model.rules[1].model_dump() == {"target": "think", "value": False}


def test_binding_rules_union_keeps_every_json_value_type():
    """M1 transport side: str/bool/int/float/list/dict/None stay native."""
    BindingRules = _rules_model()
    values: dict[str, Any] = {
        "s": "x", "b": True, "i": 3, "f": 1.5,
        "l": [1, "x", {"k": 1}], "d": {"a": [1]}, "z": None,
    }
    model = BindingRules(
        rules=[{"target": f"t_{k}", "value": v} for k, v in values.items()]
    )
    for (k, v), rule in zip(values.items(), model.rules):
        assert rule.model_dump() == {"target": f"t_{k}", "value": v}, (
            f"value {k!r} must survive the union verbatim"
        )


def test_binding_rules_mixed_rule_rejected():
    """Mixed rule (sources AND value) → ValidationError (extra_forbidden).

    RED (repro): extra=ignore parsed it as a copy rule and dropped the
    ``value`` — the union with extra=forbid refuses it.
    """
    BindingRules = _rules_model()
    with pytest.raises(pydantic.ValidationError) as e:
        BindingRules(rules=[{"sources": ["task"], "target": "x", "value": "y"}])
    assert "extra_forbidden" in str(e.value)


def test_binding_rules_unknown_key_rejected():
    """extra=forbid on both variants: an unknown key is never ignored."""
    BindingRules = _rules_model()
    with pytest.raises(pydantic.ValidationError):
        BindingRules(rules=[{"sources": ["task"], "target": "x", "zzz": 1}])
    with pytest.raises(pydantic.ValidationError):
        BindingRules(rules=[{"target": "x", "value": 1, "sources": ["a"]}])
    with pytest.raises(pydantic.ValidationError):
        BindingRules(rules=[{"target": "x"}])  # value-less const missing value


def test_binding_rules_rule_count_cap_64():
    """M7: rules carries max_length=64 (0 and >64 rejected; 64 accepted)."""
    BindingRules = _rules_model()
    many = [{"sources": ["task"], "target": f"t{i}"} for i in range(64)]
    source_names = [f"src{i}" for i in range(64)]
    many_with_sources = [
        {"sources": [s], "target": f"t{i}"} for i, s in enumerate(source_names)
    ]
    assert BindingRules(rules=many_with_sources).rules and len(
        BindingRules(rules=many).rules) == 64, "64 rules are accepted"
    with pytest.raises(pydantic.ValidationError):
        BindingRules(rules=[])  # min_length=1 (T-189-era contract, unchanged)
    with pytest.raises(pydantic.ValidationError):
        BindingRules(rules=many + [{"sources": ["x"], "target": "over"}])
    with pytest.raises(pydantic.ValidationError):
        BindingRules(rules=[{"sources": ["task"], "target": "t0", "value": 1}])


def test_binding_rules_separator_cap_64():
    BindingRules = _rules_model()
    assert BindingRules(rules=[{"sources": ["task"], "target": "task"}],
                        separator="s" * 64).separator == "s" * 64
    with pytest.raises(pydantic.ValidationError):
        BindingRules(rules=[{"sources": ["task"], "target": "task"}],
                     separator="s" * 65)


# ── Core form authority (M1, uniform on the core side) ──────────────


def test_validate_binding_form_accepts_object_and_array_consts():
    """M1 core side: list/dict consts validate verbatim (single authority)."""
    form = validate_binding_form(
        {"rules": [{"target": "cfg", "value": {"k": [1]}},
                   {"target": "items", "value": [1, "x"]}]}
    )
    assert form["rules"] == [
        {"target": "cfg", "value": {"k": [1]}},
        {"target": "items", "value": [1, "x"]},
    ]


def test_validate_binding_form_rejects_non_json_const():
    """Non-JSON instances stay rejected (cannot arrive via the HTTP body)."""
    for bad in ({"a", 1}, b"bytes", object()):
        with pytest.raises(ForwardRoutingError) as e:
            validate_binding_form({"rules": [{"target": "cfg", "value": bad}]})
        assert "JSON" in str(e.value.message)
    # Mixed form stays a form error in the core authority, too.
    with pytest.raises(ForwardRoutingError):
        validate_binding_form(
            {"rules": [{"sources": ["task"], "target": "x", "value": "y"}]}
        )


def test_apply_binding_writes_object_and_array_consts_natively():
    out = apply_binding(
        {"taskprompt": "T"},
        [{"target": "cfg", "value": {"k": 1}},
         {"target": "items", "value": [1, "x"]},
         {"sources": ["taskprompt"], "target": "task"}],
        "\n\n",
    )
    assert out == {"cfg": {"k": 1}, "items": [1, "x"], "task": "T"}


def test_check_binding_schema_array_and_object_match_consts():
    """check_binding_schema (:552-553 side) matches list/dict consts — the
    type match the PUT ladder runs on the new M1 values."""
    from relay_server.core.forward import check_binding_schema

    schema = {"fields": {
        "cfg": {"type": "object"},
        "items": {"type": "array"},
    }}
    # No raise on matches:
    check_binding_schema(
        schema,
        [{"target": "cfg", "value": {}}, {"target": "items", "value": []}],
    )
    # Cross-type stays a 422:
    with pytest.raises(ForwardRoutingError) as e:
        check_binding_schema(schema, [{"target": "cfg", "value": [1]}])
    assert "must match declared type 'object'" in str(e.value.message)