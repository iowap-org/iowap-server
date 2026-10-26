"""T-252: binding const-rules — every source to every target, generically.

Ronny (2026-10-26): the binding editor needs TARGET fields that carry a
FIXED value selected in the dashboard („think anhacken", model dropdown) —
not only field-to-field copies. Contract:

- A rule is EITHER ``{sources: [str, …], target: str}`` (T-247 copy/join)
  OR a const-rule ``{target: str, value: <primitive>}`` — ``sources``
  absent means const; ``sources`` AND ``value`` together are invalid;
  ``value`` alone with ``sources`` present is invalid.
- Const value must be a JSON primitive (str / bool / int / float, not
  None-with-absent — ``null`` value is an explicit write of ``null``,
  allowed); objects/arrays are rejected (binding stays scalar).
- PUT hard check (generic): the rule's ``target`` must exist in the
  pinned node's (or slot's) ``input_schema`` (T-247 form); WHEN the
  field spec carries a ``type`` and the rule is const, the value type
  must match — string→str, boolean→bool, integer→int, number→int|float;
  unknown/absent spec-type = name-only check (T-247 form).
- apply_binding: a const rule WRITES its value natively and marks the
  target written (sweep keeps it); const rules never consume (no
  sources); a const rule may combine with copy/join rules (targets stay
  disjoint); chaining is unchanged.
"""

import os
import tempfile
from pathlib import Path

import pytest

os.environ["RELAY_DB_PATH"] = ""

from relay_server.core.db import init_db
from relay_server.core.forward import (
    ForwardRoutingError,
    apply_binding,
    validate_binding_form,
)


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        from relay_server.config import settings

        settings.db_path = Path(tmp) / "t252.db"
        init_db()
        yield


SEP = "\n\n"


# ── Form-Guard (validate_binding_form) ─────────────────────────────


def test_const_rule_form_valid():
    form = validate_binding_form({"rules": [{"target": "think", "value": False}]})
    assert form["rules"] == [{"target": "think", "value": False}]
    assert form["separator"] == SEP


def test_const_rule_explicit_null_value_is_a_write():
    form = validate_binding_form({"rules": [{"target": "schema", "value": None}]})
    assert form["rules"] == [{"target": "schema", "value": None}]


def test_const_rule_with_sources_is_invalid():
    with pytest.raises(ForwardRoutingError) as e:
        validate_binding_form(
            {"rules": [{"sources": ["taskprompt"], "target": "task", "value": "x"}]}
        )
    assert "sources' and 'value'" in str(e.value.message) or "sources" in str(e.value.message)


def test_const_rule_value_must_be_primitive():
    for bad in ({"a": 1}, ["x"], {"deep": {"y": 2}}):
        with pytest.raises(ForwardRoutingError) as e:
            validate_binding_form({"rules": [{"target": "files", "value": bad}]})
        assert "primitive" in str(e.value.message) or "object" in str(e.value.message)


def test_const_rule_mixed_forms_in_one_binding_ok_but_disjoint_targets():
    form = validate_binding_form(
        {
            "rules": [
                {"sources": ["systemprompt"], "target": "system"},
                {"target": "think", "value": False},
            ]
        }
    )
    assert form["rules"][0]["target"] == "system"
    assert form["rules"][1] == {"target": "think", "value": False}


# ── apply_binding semantics (generic) ──────────────────────────────


def test_const_rule_writes_value_and_sweep_keeps_it():
    out = apply_binding(
        {"systemprompt": "SYS", "taskprompt": "TASK", "original_request": "r"},
        [{"sources": ["systemprompt"], "target": "system"},
         {"target": "think", "value": False}],
        SEP,
    )
    # taskprompt is RESERVED (KNOWN_FLOW_PROMPT_FIELDS) — survives the
    # sweep unless a rule consumes it; the const rule doesn't.
    assert out == {"system": "SYS", "think": False, "taskprompt": "TASK"}


def test_const_rule_combines_with_join_rule():
    out = apply_binding(
        {"systemprompt": "A", "taskprompt": "B"},
        [{"sources": ["systemprompt", "taskprompt"], "target": "task"},
         {"target": "think", "value": False},
         {"target": "model", "value": "local-model"}],
        SEP,
    )
    assert out == {"task": "A\n\nB", "think": False, "model": "local-model"}


def test_const_rule_boolean_native_not_stringified():
    out = apply_binding({}, [{"target": "think", "value": True}], SEP)
    assert out == {"think": True}


def test_const_rule_chain_then_copy_reads_const_target():
    # chaining: a later copy-rule may read a const-written target (order).
    out = apply_binding(
        {"taskprompt": "TASK"},
        [{"target": "task", "value": "ROLE"},
         {"sources": ["taskprompt"], "target": "question"}],
        SEP,
    )
    assert out == {"task": "ROLE", "question": "TASK"}


# ── PUT hard check (generic type match) ────────────────────────────


def _put_ladder_helper():
    """PUT exists? → cheap import-level proof the ladder helper is importable."""
    from relay_server.core.forward import check_binding_schema

    return check_binding_schema


def test_const_value_type_must_match_field_spec_type():
    check_binding_schema = _put_ladder_helper()
    schema = {"fields": {"think": {"type": "boolean"}, "model": {"type": "string"},
                         "limit": {"type": "integer"}, "files": {"type": "array"}}}
    check_binding_schema(schema, [{"target": "think", "value": False}])  # ok
    with pytest.raises(ForwardRoutingError) as e:
        check_binding_schema(schema, [{"target": "think", "value": "true"}])  # WRONG type
    assert "boolean" in str(e.value.message)
    with pytest.raises(ForwardRoutingError) as e:
        check_binding_schema(schema, [{"target": "model", "value": None}])  # None ≠ string
    check_binding_schema(schema, [{"target": "limit", "value": 7}])  # int ok
    with pytest.raises(ForwardRoutingError) as e:
        check_binding_schema(schema, [{"target": "files", "value": False}])


def test_copy_rule_unaffected_by_type_match():
    check_binding_schema = _put_ladder_helper()
    schema = {"fields": {"task": {"type": "string"}}}
    # source-copy rules carry no value → only the name check applies
    check_binding_schema(schema, [{"sources": ["systemprompt"], "target": "task"}])


def test_schema_without_type_skips_type_match():
    check_binding_schema = _put_ladder_helper()
    schema = {"fields": {"think": {"name": "think"}}}  # spec has NO type
    check_binding_schema(schema, [{"target": "think", "value": 3.14}], )