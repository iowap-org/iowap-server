"""T-235: the flow plan-split prompt fields in the T-247 reserved mirror.

The two submit-side split fields ("systemprompt"/"taskprompt" — the
iowap-flow design, .hermes/pipeline/t235-flow-prompt-split/design.md §10.1)
join KNOWN_FLOW_PROMPT_FIELDS (core/forward.py) as reserved prompt-channel
fields: they survive the unbound-fields sweep of apply_binding unless a rule
consumes them. Pure apply_binding unit tests — no DB, no client.
"""
from relay_server.core.forward import KNOWN_FLOW_PROMPT_FIELDS, apply_binding

JOIN = "\n\n"


def test_split_fields_survive_empty_rules():
    """Test T-235/1 — with NO fired rule, ONLY the reserved set survives: the
    split fields are reserved after the mirror extension (design §10.1)."""
    # Arrange (apply_binding is pure)
    payload = {"systemprompt": "S", "taskprompt": "P"}
    # Act
    out = apply_binding(payload, rules=[], separator=JOIN)
    # Assert
    assert out == {"systemprompt": "S", "taskprompt": "P"}


def test_split_fields_survive_unbound_under_rule():
    """A fired rule consumes ITS sources only; unbound split fields survive
    the sweep (design §3.2.2/T15)."""
    # Arrange
    payload = {"systemprompt": "S", "taskprompt": "P", "x": "D"}
    rules = [{"sources": ["x"], "target": "o"}]
    # Act
    out = apply_binding(payload, rules=rules, separator=JOIN)
    # Assert
    assert out == {"systemprompt": "S", "taskprompt": "P", "o": "D"}


def test_known_mirror_contains_legacy_prompt_keys():
    """Guard: the extended mirror still covers the verbatim PROMPT_KEYS set
    (the D6 chain mirror stays intact — design §10.1)."""
    # Arrange / Act / Assert
    assert frozenset({"task", "prompt", "question", "message", "input"}) <= KNOWN_FLOW_PROMPT_FIELDS