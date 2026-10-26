"""T-253: value-slot defaults ride into the submit payload (Ronny's
original slot-vision, deferred from T-247 D-15).

- The submitter's declared VALUE slots (slot_type text/bool with a
  stored value) inject as payload defaults — ref_slot/ref_field map them
  to the routing slot's payload; ref_field missing -> the slot's own name.
- The submitted payload WINS (setdefault): a field the runner sends is
  never overwritten.
- Booleans inject natively; the writer's "null" text = no default.
- Binding projects AFTER injection; the sweep still applies (reserved
  fields survive, unconsumed non-reserved defaults are dropped).
"""

import json
import tempfile
from pathlib import Path

import pytest
from relay_server.config import settings
from relay_server.core.db import init_db, q
from relay_server.core.forward import (
    replace_forward_slots,
    resolve_forward_stages,
    upsert_forward_mapping,
)

SEP = "\n\n"
SPEC_SCHEMA = {
    "fields": {
        "task": {"name": "task", "type": "string", "required": True, "description": "The task."},
        "system": {"name": "system", "type": "string", "required": True, "description": "The ROLE."},
        "think": {"name": "think", "type": "boolean", "required": False, "description": "think=false mandatory."},
        "model": {"name": "model", "type": "string", "required": False, "description": "Allowed values: qwen3.5:2b (default)."},
    }
}


@pytest.fixture()
def fresh_node_and_mapping():
    """NODEA declares plan + two value slots; mapping plan -> model.specialist."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t253.db"
        init_db()
        from relay_server.core.db import get_conn

        conn = get_conn()
        # keins conn.close() — get_conn() ist der geteilte Handle (T-258-Muster)
        caps = json.dumps([{"name": "model.specialist"}])
        conn.execute(
            q("INSERT INTO nodes (node_id, node_name, status, capabilities,"
              " registered_at, last_seen)"
              " VALUES ('NODEA', 'test-flow', 'online', '[]',"
              " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')")
        )
        conn.execute(
            q("INSERT INTO nodes (node_id, node_name, status, capabilities,"
              " registered_at, last_seen)"
              " VALUES ('TARGETN', 'specialist', 'online', ?,"
              " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
              (caps,))
        )
        conn.execute(
            q("INSERT INTO node_capabilities (node_id, capability_name,"
              " capability_version, input_schema, available, updated_at)"
              " VALUES ('TARGETN', 'model.specialist', '1.0.0', ?, 1,"
              " '2026-01-01T00:00:00+00:00')",
              (json.dumps(SPEC_SCHEMA),))
        )
        conn.commit()
        replace_forward_slots(
            "NODEA",
            [
                {"slot": "plan", "type": "forward", "label": "Planner",
                 "input_schema": {"fields": {
                     "systemprompt": {"name": "systemprompt", "type": "string",
                                      "description": "Statischer Rollen-/Format-Vertrag."},
                     "taskprompt": {"name": "taskprompt", "type": "string",
                                    "description": "Der Task-Text + caps + Feedback."},
                 }},
                },
                # KEIN ref: der Default gilt fuer ALLE forward-Slots des
                # Nodes (der "*" case); der Runner-submit gewinnt (setdefault).
                {"slot": "systemprompt", "type": "text", "label": "System prompt",
                 "value": "ROLE: plan-compiler."},
                {"slot": "think", "type": "bool", "label": "Think",
                 "value": False},
                {"slot": "taskprompt", "type": "text", "label": "Task prompt"},
            ],
        )
        upsert_forward_mapping(
            "NODEA", "plan", "model.specialist", "TARGETN",
            {"rules": [{"sources": ["systemprompt"], "target": "system"},
                       {"sources": ["taskprompt"], "target": "task"}],
             "separator": SEP},
        )
        yield


def _run(payload: dict, slot: str = "plan"):
    stages = [{"stage_name": "planning", "capability": "forward",
               "payload": {"forward_slot": slot, **payload}}]
    resolve_forward_stages("NODEA", stages)
    return stages[0]["payload"]


def test_value_slots_inject_as_defaults(fresh_node_and_mapping):
    # Node-receives form: the DEFAULT (systemprompt) fed the system rule;
    # the submitted taskprompt fed the task rule; consumed sources and the
    # unconsumed non-reserved bool default do NOT reach the node payload.
    out = _run({"taskprompt": "Plan the thing."})
    assert out["system"] == "ROLE: plan-compiler."         # default -> rule -> node
    assert out["task"] == "Plan the thing."                # submit -> rule -> node
    assert "systemprompt" not in out                       # consumed by the rule
    assert "taskprompt" not in out                         # consumed by the rule
    assert "think" not in out                              # injected, unconsumed, non-reserved -> swept


def test_submitted_payload_wins_over_default(fresh_node_and_mapping):
    out = _run({"systemprompt": "RUNNER-SIDE-ROLE", "taskprompt": "Plan A."})
    assert out["system"] == "RUNNER-SIDE-ROLE"             # payload wins


def test_declared_value_slot_without_value_never_injects(fresh_node_and_mapping):
    out = _run({})
    assert "taskprompt" not in out  # declared, no value -> chip only


def test_sweep_drops_unconsumed_non_reserved_default(fresh_node_and_mapping):
    """A bool default no rule consumes and NOT in the reserved set is
    dropped by the sweep under a binding; reserved prompt fields survive."""
    upsert_forward_mapping(
        "NODEA", "plano2", "model.specialist", "TARGETN",
        {"rules": [{"sources": ["taskprompt"], "target": "task"}], "separator": SEP},
    )
    out = _run({"taskprompt": "Plan B."}, slot="plano2")
    assert out["task"] == "Plan B."                       # rule consumed
    assert out["systemprompt"] == "ROLE: plan-compiler."  # injected + reserved survives
    assert "taskprompt" not in out                        # consumed by the rule
    assert "think" not in out                             # injected, unconsumed, non-reserved -> swept


def test_const_rules_complete_the_ronny_case(fresh_node_and_mapping):
    upsert_forward_mapping(
        "NODEA", "plano3", "model.specialist", "TARGETN",
        {"rules": [{"sources": ["taskprompt"], "target": "task"},
                   {"target": "think", "value": False},
                   {"target": "model", "value": "qwen3.5:2b"}],
         "separator": SEP},
    )
    out = _run({"taskprompt": "Plan C."}, slot="plano3")
    assert out["task"] == "Plan C."
    assert out["think"] is False                            # const rule kept (written)
    assert out["model"] == "qwen3.5:2b"                     # const rule kept (written)
    assert "taskprompt" not in out                          # consumed by the rule
    assert out["systemprompt"] == "ROLE: plan-compiler."   # injected default + reserved survives
