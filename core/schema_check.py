"""T-274 P7: submit-time schema warn path — match, mark, never gate.

The node heartbeats ``schema_hash`` (SHA-256 over the canonical
input_schema JSON) next to ``input_schema`` (models/discovery heartbeat
field ``CapabilityStatus.schema_hash``, persisted in
``node_capabilities.schema_hash`` via ``sync_node_capabilities``). At
submit time the relay checks the stage payload against the schema stored
from the LAST heartbeat of that capability:

* violation  → the submit succeeds (the task runs normally) but the stage
  carries a ``schema_violation`` warn field, state ``schema_violation``,
  listing the violations.
* no fresh basis → the capability was heartbeated without
  ``input_schema``, without ``schema_hash`` (a schema alone cannot be
  vouched for), or the stage references an unknown capability entirely:
  state ``unvalidated`` with ``reason`` ``no_schema_basis`` /
  ``capability_unknown`` — visibly marked instead of warned-false.
* validated  → no warn at all (absence of the field = validated).

The warn path NEVER fails the submit (no 422, no 404 for schema reasons)
and never mutates the payload — the relay matches and routes only.

K3 analog: warnings carry the violations (field/issue/detail) only, no
payload echo.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from relay_server.core.db import get_conn, q

# Violation issue codes surfaced in the warn field.
ISSUE_REQUIRED = "required"
ISSUE_TYPE = "type"
ISSUE_UNEXPECTED = "unexpected"
ISSUE_ENUM = "enum"
ISSUE_BOUNDS = "bounds"

_JSON_TYPE_MAP = {
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "array": list,
    "object": dict,
}

WARN_STATE_VIOLATION = "schema_violation"
WARN_STATE_UNVALIDATED = "unvalidated"


def get_schema_basis(capability: str) -> Optional[Dict[str, Any]]:
    """Load the schema basis the relay can vouch for (T-274 P7).

    Reads ``node_capabilities`` (the normalized index, rebuilt per
    heartbeat replace). Returns:

    * ``None`` — unknown capability (no row at all for the name).
    * ``{"input_schema": <dict or None>, "schema_hash": <str or None>}``
      — the capability is known; ``input_schema`` is non-None only when
      the last heartbeat shipped BOTH the schema and its hash (the
      freshness signal: a schema without a hash cannot be vouched for
      and reads as no basis).

    When several nodes advertise the capability, the first vouchable
    basis wins (validation has to be possible, not node-pinned — the
    scheduler routes stages to any capable provider).
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            q(
                "SELECT input_schema, schema_hash FROM node_capabilities "
                "WHERE capability_name = ?",
                (capability,),
            )
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    import json

    basis: Optional[Dict[str, Any]] = None
    for row in rows:
        schema_raw = row["input_schema"]
        schema_hash = row["schema_hash"]
        if not schema_raw or not schema_hash:
            continue
        try:
            schema = json.loads(schema_raw)
        except Exception:
            continue
        basis = {"input_schema": schema, "schema_hash": schema_hash}
        break
    if basis is not None:
        return basis
    return {"input_schema": None, "schema_hash": None}


def check_payload(
    payload: Dict[str, Any], schema: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Check a payload against a capability input_schema (warn items only).

    The schema is the node-owned ``inputs``/``fields`` shape
    (``{"type": ..., "required": ...}``, same form
    :class:`CapabilityInputSchema` parses). Violations are structured
    warn items ``{"field", "issue", "detail"}`` — never raised, never
    422. Rules mirror the existing server-side validator
    (models/capability.py): required fields present, values type- and
    enum-conform, bounds kept, payload keys that the schema does not
    declare are unexpected.
    """
    violations: List[Dict[str, Any]] = []
    if not isinstance(payload, dict):
        return [{
            "field": "",
            "issue": ISSUE_UNEXPECTED,
            "detail": "Payload must be a dictionary.",
        }]
    raw = schema.get("fields", schema) if isinstance(schema, dict) else {}
    fields: Dict[str, Dict[str, Any]] = {}
    if isinstance(raw, dict):
        for key, raw_field in raw.items():
            name = (
                raw_field.get("name") or key
                if isinstance(raw_field, dict)
                else str(key)
            )
            fields[name] = raw_field if isinstance(raw_field, dict) else {}
    elif isinstance(raw, list):
        # Positional form: derive names the same way from_dict does.
        for i, raw_field in enumerate(raw):
            name = raw_field.get("name") or f"field_{i}" if isinstance(raw_field, dict) else f"field_{i}"
            fields[name] = raw_field if isinstance(raw_field, dict) else {}

    for key in payload:
        if key not in fields:
            violations.append({
                "field": str(key),
                "issue": ISSUE_UNEXPECTED,
                "detail": f"Unexpected field '{key}' in payload.",
            })

    for name, fld in fields.items():
        required = bool(fld.get("required", False))
        default = fld.get("default", None)
        if name not in payload or payload[name] is None:
            if required and default is None:
                violations.append({
                    "field": name,
                    "issue": ISSUE_REQUIRED,
                    "detail": f"Field '{name}' is required.",
                })
            continue
        value = payload[name]

        expected_type = fld.get("type")
        if expected_type in _JSON_TYPE_MAP:
            expected = _JSON_TYPE_MAP[expected_type]
            # bool is a subclass of int in Python — a boolean payload value
            # satisfies neither integer nor number gates.
            if expected_type in ("integer", "number"):
                invalid = isinstance(value, bool) or not isinstance(value, expected)
            else:
                invalid = not isinstance(value, expected)
            if invalid:
                violations.append({
                    "field": name,
                    "issue": ISSUE_TYPE,
                    "detail": (
                        f"Field '{name}': value {value!r} must be of type"
                        f" {expected_type}."
                    ),
                })
                continue

        enum = fld.get("enum")
        if enum is not None and value not in enum:
            violations.append({
                "field": name,
                "issue": ISSUE_ENUM,
                "detail": f"Field '{name}': value {value!r} not in {enum!r}.",
            })

        ge, le = fld.get("ge"), fld.get("le")
        if ge is not None or le is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                violations.append({
                    "field": name,
                    "issue": ISSUE_TYPE,
                    "detail": f"Field '{name}': value {value!r} must be numeric.",
                })
            else:
                if ge is not None and value < ge:
                    violations.append({
                        "field": name,
                        "issue": ISSUE_BOUNDS,
                        "detail": f"Field '{name}': value {value!r} must >= {ge}.",
                    })
                if le is not None and value > le:
                    violations.append({
                        "field": name,
                        "issue": ISSUE_BOUNDS,
                        "detail": f"Field '{name}': value {value!r} must <= {le}.",
                    })
    return violations


def build_stage_warn(
    capability: str, payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Build the T-274 P7 schema warn for one submitted stage (or None).

    States:
    * ``schema_violation`` — a vouchable basis exists and the payload does
      not conform; ``violations`` lists the items.
    * ``unvalidated`` / ``no_schema_basis`` — the capability is heartbeated
      without a schema+hash pair the relay can vouch for.
    * ``unvalidated`` / ``capability_unknown`` — the stage references a
      capability no row in the index knows.

    ``None`` = validated. This function never raises and never gates.
    """
    basis = get_schema_basis(capability)
    if basis is None:
        return {
            "state": WARN_STATE_UNVALIDATED,
            "capability": capability,
            "reason": "capability_unknown",
        }
    if basis.get("input_schema") is None:
        return {
            "state": WARN_STATE_UNVALIDATED,
            "capability": capability,
            "reason": "no_schema_basis",
        }
    violations = check_payload(payload if isinstance(payload, dict) else {}, basis["input_schema"])
    if not violations:
        return None
    return {
        "state": WARN_STATE_VIOLATION,
        "capability": capability,
        "schema_hash": basis.get("schema_hash"),
        "violations": violations,
    }
