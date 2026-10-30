"""Forward-routing resolution (T-Forward) — (node_id, slot) → target capability.

On submit the relay recognizes a stage with capability ``forward`` carrying
``payload["forward_slot"]``, resolves the submitter's mapping and rewrites
the stage capability in place (submit-time redirect). ``submitted_by`` stays
the originating node (T-227); the first claim of the single-stage rewritten
task flips ``owner_node_id`` (scheduler T-227 semantics, unchanged here).

The ``forward`` pseudo-capability is never advertised by nodes; mappings are
managed via ``api/v2/forward.py`` (admin only). Contracts:
``.hermes/pipeline/t-forward/design.md`` §3.1.
"""

import json
import re
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import (  # noqa: UP035  (frozen contract: stub imports kept verbatim)
    Any,
    Final,
    List,
    Mapping,
    Optional,
    Union,
)

from relay_server.core.db import DBConn, get_conn, q
from relay_server.models.capability import CapabilityInputSchema
from sqlalchemy import exc as sa_exc

FORWARD_CAPABILITY: Final[str] = "forward"
SLOT_MAX_CHARS: Final[int] = 64
_SLOT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

SLOT_TYPE_FORWARD: Final[str] = "forward"
SLOT_TYPE_TEXT: Final[str] = "text"
SLOT_TYPE_BOOL: Final[str] = "bool"
SLOT_TYPES: Final[frozenset[str]] = frozenset(
    {SLOT_TYPE_FORWARD, SLOT_TYPE_TEXT, SLOT_TYPE_BOOL}
)
DEFAULT_SLOT_TYPE: Final[str] = SLOT_TYPE_FORWARD

DEFAULT_BINDING_SEPARATOR: Final[str] = "\n\n"

# T-247 §10: the known Flow prompt fields — the reserved set mirrors the live
# handler prompt channel: the D6 PROMPT_KEYS of
# iowap-flow/handlers/agent_ai.py:44 verbatim ("task", "prompt", "question",
# "message", "input") PLUS the T-235 plan-split channel ("systemprompt",
# "taskprompt"). These fields survive the unbound-fields sweep at submit
# (apply_binding, design §3.3) so a handler's prompt channel cannot be eaten
# by an unbinding rewrite; the full rationale + rejection of alternatives
# lives in design.md §10.
KNOWN_FLOW_PROMPT_FIELDS: Final[frozenset[str]] = frozenset(
    {"task", "prompt", "question", "message", "input", "systemprompt", "taskprompt"}
)

BINDING_STRING_MAX_CHARS: Final[int] = 255  # per source/target name; parity with target_capability cap


class ForwardRoutingError(Exception):
    """Refusal with an HTTP-mappable error code (T-Forward)."""

    def __init__(self, code: str, http_status: int, message: str) -> None:
        self.code = code
        self.http_status = http_status
        self.message = message
        super().__init__(f"{code}: {message}")


def is_forward_stage(stage: Mapping[str, Any]) -> bool:
    """True when the stage targets the ``forward`` pseudo-capability (exact match)."""
    return stage.get("capability") == FORWARD_CAPABILITY


def normalize_slot(value: Any) -> str:
    """Normalize a forward slot (trim; ``1``/``a``/``s-2``; ≤ 64 chars).

    Raises ``ForwardRoutingError("forward_mapping_invalid", 422, …)`` for
    bools, non-str/int values, empty/whitespace strings, inner whitespace,
    oversized or disallowed characters. Contracts: design.md §3.1.b.
    """

    def _invalid(msg: str) -> ForwardRoutingError:
        return ForwardRoutingError("forward_mapping_invalid", 422, msg)

    # Bools are an int subclass and must never coerce ("1" vs "True").
    if isinstance(value, bool):
        raise _invalid("forward slot must be a string, got a bool")
    if isinstance(value, int):
        value = str(value)
    elif not isinstance(value, str):
        raise _invalid(f"forward slot must be a string, got {type(value).__name__}")

    slot = value.strip()
    if not slot:
        raise _invalid("forward slot must not be empty")
    if re.search(r"\s", slot):
        raise _invalid("forward slot must not contain whitespace")
    if not 1 <= len(slot) <= SLOT_MAX_CHARS:
        raise _invalid(f"forward slot must be 1..{SLOT_MAX_CHARS} chars")
    if not _SLOT_PATTERN.fullmatch(slot):
        raise _invalid("forward slot allows only letters, digits, '_' and '-'")
    return slot


def normalize_slot_type(value: Any) -> str:
    """Resolve the declared slot type against the registry (T-245 C1).

    ``None``/absent → ``DEFAULT_SLOT_TYPE`` (Bestandsschutz: old
    declarations never send a type). Explicit values are matched EXACTLY
    (case-sensitive, after ``str``-strip) against ``SLOT_TYPES``; unknown
    → ``ForwardRoutingError("forward_slot_invalid", 422, …)`` — loud,
    node-owned metadata error. Bools and non-str values are invalid as
    well (a bool is never a type; same guard as normalize_slot,
    core/forward.py:61). Contracts: design.md §3.2.
    """
    def _invalid(msg: str) -> ForwardRoutingError:
        return ForwardRoutingError("forward_slot_invalid", 422, msg)

    if value is None:
        return DEFAULT_SLOT_TYPE
    # Bools are an int subclass and must never coerce; anything non-str is
    # a mis-typed declaration (loud, node-owned metadata error).
    if isinstance(value, bool) or not isinstance(value, str):
        raise _invalid(f"slot type must be a string, got {type(value).__name__}")
    slot_type = value.strip()
    if not slot_type:
        raise _invalid("slot type must not be empty when declared")
    if slot_type not in SLOT_TYPES:
        # Exact registry match — no case-folding ("Forward"/"TEXT" → 422).
        raise _invalid(
            f"unknown slot type '{slot_type}'"
            f" (known: {', '.join(sorted(SLOT_TYPES))})"
        )
    return slot_type


def normalize_slot_value(slot_type: str, value: Any) -> Union[str, bool, None]:  # noqa: UP007  (frozen signature: Union)
    """Validate the typed ``value`` field of a declaration (T-245 C4).

    Per ``slot_type``: ``text`` → ``value`` must be ``str``; ``bool`` →
    ``value`` must be a REAL bool (``"true"``, ``1`` →
    ``ForwardRoutingError("forward_slot_invalid", 422, …)`` — bools are an
    int subclass and must never coerce, core/forward.py:61 precedent).
    ``type: forward`` FORBIDS ``value`` (the routing target lives in
    ``forward_mappings``; see Decision D-02). ``None`` → ``None`` for every
    type (a value-less value-slot is an empty declaration, allowed). The
    native value is returned unchanged — JSON serialization is persistence
    detail of ``replace_forward_slots``. Contracts: design.md §3.2/§4.
    """
    def _invalid(msg: str) -> ForwardRoutingError:
        return ForwardRoutingError("forward_slot_invalid", 422, msg)

    if value is None:
        return None
    if slot_type == SLOT_TYPE_TEXT:
        if not isinstance(value, str):
            raise _invalid(
                f"text slot value must be a string, got {type(value).__name__}"
            )
        return value
    if slot_type == SLOT_TYPE_BOOL:
        # Bools are an int subclass: "true" and 1 must never coerce (C4).
        if not isinstance(value, bool):
            raise _invalid(
                f"bool slot value must be a real bool, got {type(value).__name__}"
            )
        return value
    # slot_type == SLOT_TYPE_FORWARD — a value is forbidden there (D-02):
    # the routing target lives in forward_mappings, not on the slot row.
    raise _invalid(
        "forward slots must not carry a 'value'"
        " — the routing target lives in forward_mappings"
    )


def normalize_slot_ref(entry: Mapping[str, Any], slot_type: str) -> Optional[tuple[str, str]]:  # noqa: UP045  (frozen signature: Optional)
    """Normalize ``ref_slot``/``ref_field`` of a declaration (T-245 C4).

    A ref pair is legal ONLY on value slots (``text``/``bool``) and must be
    given WHOLE: ``ref_slot`` XOR ``ref_field`` → 422
    ``forward_slot_invalid`` (halber Ref). On ``type: forward`` any ref →
    422. Returns ``(ref_slot, ref_field)`` when a pair is declared —
    ``ref_slot`` validated with the ``normalize_slot`` charset/length rules
    (core/forward.py:49) with the error code remapped to
    ``forward_slot_invalid`` — or ``None`` when no ref is declared.
    Whether ``ref_field`` exists inside the referenced slot's
    ``input_schema`` is NOT validated here (DEFERRED to T-247).
    Contracts: design.md §3.2/§4.
    """
    def _invalid(msg: str) -> ForwardRoutingError:
        return ForwardRoutingError("forward_slot_invalid", 422, msg)

    raw_slot = entry.get("ref_slot")
    raw_field = entry.get("ref_field")
    if slot_type == SLOT_TYPE_FORWARD:
        # Routing handles carry no refs — the target lives in forward_mappings.
        if raw_slot is not None or raw_field is not None:
            raise _invalid(
                "forward slots must not carry a ref"
                " — the routing target lives in forward_mappings"
            )
        return None
    # Value slots accept a ref, but only as a WHOLE pair (no half refs).
    # ref_slot goes through normalize_slot (int coercion allowed there);
    # ref_field has no normalizer and the FROZEN return type is str, so a
    # non-string ref_field counts as absent → the pair is half → loud 422.
    has_slot = raw_slot is not None
    has_field = isinstance(raw_field, str)
    if has_slot != has_field:
        raise _invalid("a ref must declare both ref_slot and ref_field together")
    if not has_slot:
        return None
    try:
        ref_slot = normalize_slot(raw_slot)
    except ForwardRoutingError as exc:
        # Same charset/length rules as normalize_slot, remapped to the
        # declaration error code (design.md §3.2).
        raise _invalid(exc.message) from exc
    return (ref_slot, raw_field)


def validate_binding_form(binding: Any) -> Optional[dict[str, Any]]:  # noqa: UP045  (frozen signature: Optional)
    """Validate the FORM of a forward-mapping binding (T-247 C1/C2).

    ``None`` (field omitted / explicit null) returns ``None`` = legacy
    passthrough — callers short-circuit on falsy before the semantic
    checks below. Otherwise the value must be a dict exactly of the
    Ronny form: ``{"rules": [{"sources": [str, …], "target": str},
    {"target": str, "value": <JSON value>}, …], "separator": str}``.
    Rules: 1..N entries; each rule a dict with exactly the keys
    ``sources`` + ``target`` (copy rule) OR ``target`` + ``value``
    (const rule, T-252; NO key mix, which would be ambiguous — loud);
    ``sources`` a non-empty
    list of distinct strings, each 1..BINDING_STRING_MAX_CHARS chars
    with NO leading/trailing whitespace (a padded name could never match
    an exact payload key — loud at PUT instead of a silent dead rule);
    ``target`` the same string rules; ``target`` names are DISJOINT
    across rules (a duplicate would make the projection order-dependent,
    plan C2); an optional ``separator`` must be a str (any content,
    incl. "" — adjacent join is a legitimate form; payload bloat is
    bounded by the task payload cap at submit, models/task.py:34-41).
    T-255 (M1): a const ``value`` may be every JSON value —
    str/bool/int/float AND list/dict (uniform with the transport models
    ConstRule/CopyRule and with check_binding_schema's array/object
    branches; the T-254-r2 editor offers object-typed consts). A None
    value is an EXPLICIT write of null (distinct from omitting the key).
    The normalized form is returned as
    ``{"rules": [{"sources": […], "target": …}, …], "separator": str}``
    with an absent ``separator`` defaulted to DEFAULT_BINDING_SEPARATOR.
    Shared by the PUT ladder (core/forward.py, design §4 step 8) and the
    submit-time stored-JSON re-parse (design §6) — single semantic
    authority. Violations raise ``ForwardRoutingError(
    "forward_binding_invalid", 422, …)``. Contracts: design.md §3/§4.
    """
    def _bad(msg: str) -> ForwardRoutingError:
        return ForwardRoutingError("forward_binding_invalid", 422, msg)

    if binding is None:
        return None
    if not isinstance(binding, dict):
        raise _bad(f"binding must be an object, got {type(binding).__name__}")

    rules = binding.get("rules")
    if not isinstance(rules, list) or not rules:
        raise _bad(
            "binding 'rules' must be a non-empty list, got"
            f" {type(rules).__name__ if rules is not None else 'nothing'}"
        )

    separator = DEFAULT_BINDING_SEPARATOR
    if "separator" in binding:
        raw_separator = binding["separator"]
        if not isinstance(raw_separator, str):
            raise _bad(
                "binding 'separator' must be a string, got"
                f" {type(raw_separator).__name__}"
            )
        separator = raw_separator

    def _name_violation(name: Any, what: str) -> str | None:
        """String rules for source/target names, or the first-offender msg."""
        if not isinstance(name, str):
            return f"{what} must be a string, got {type(name).__name__}"
        if not 1 <= len(name) <= BINDING_STRING_MAX_CHARS:
            return (
                f"{what} must be 1..{BINDING_STRING_MAX_CHARS} chars,"
                f" got a {len(name)}-char name"
            )
        if name.strip() != name:
            return f"{what} must not carry leading/trailing whitespace"
        return None

    normalized_rules: list[dict[str, Any]] = []
    targets_seen: set[str] = set()
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            raise _bad(f"rule {i}: must be an object, got {type(rule).__name__}")
        # T-252 const-rules: a rule is either the T-247 copy/join form
        # (exact keys sources+target) or a const rule (target + value,
        # sources absent). The FORM CLASS is decided first — the
        # per-form key checks follow; mixing 'sources' AND 'value' is a
        # form error (which semantic would win is undefined — loud).
        is_form_const = set(rule) == {"target", "value"}
        if not is_form_const and set(rule) != {"sources", "target"}:
            raise _bad(
                f"rule {i}: must carry exactly ('sources' and 'target') for a"
                " copy rule OR ('target' and 'value') for a const rule, got"
                f" {sorted(str(k) for k in rule)}"
            )
        if is_form_const:
            target = rule["target"]
            violation = _name_violation(target, f"rule {i} target")
            if violation:
                raise _bad(violation)
            if target in targets_seen:
                raise _bad(
                    f"rule {i}: duplicate target '{target}' across rules (targets"
                    " must be disjoint — a duplicate would make the projection"
                    " order-dependent)"
                )
            targets_seen.add(target)
            # const rule: writes value verbatim, consumes nothing. Value
            # may be ANY JSON value (T-255 M1: str/bool/int/float AND
            # list/dict — uniform with the transport ConstRule and
            # check_binding_schema's array/object branches; the
            # T-254-r2 editor offers object-typed consts). The None case
            # is an EXPLICIT write of null (distinct from omitting the
            # key). Non-JSON instances (direct core callers) stay loud.
            value = rule["value"]
            if value is not None and not isinstance(
                value, (str, bool, int, float, list, dict)
            ):
                raise _bad(
                    f"rule {i}: constants must be a JSON value (str/bool/"
                    f" int/float/null/list/object), got {type(value).__name__}"
                )
            normalized_rules.append({"target": target, "value": value})
            continue
        sources = rule["sources"]
        if not isinstance(sources, list) or not sources:
            raise _bad(
                f"rule {i}: 'sources' must be a non-empty list, got"
                f" {type(sources).__name__ if sources is not None else 'nothing'}"
            )
        seen_sources: set[str] = set()
        for j, source in enumerate(sources):
            violation = _name_violation(source, f"rule {i} source {j}")
            if violation:
                raise _bad(violation)
            if source in seen_sources:
                raise _bad(f"rule {i}: duplicate source '{source}'")
            seen_sources.add(source)
        target = rule["target"]
        violation = _name_violation(target, f"rule {i} target")
        if violation:
            raise _bad(violation)
        if target in targets_seen:
            raise _bad(
                f"rule {i}: duplicate target '{target}' across rules (targets"
                " must be disjoint — a duplicate would make the projection"
                " order-dependent)"
            )
        targets_seen.add(target)
        normalized_rules.append({"sources": list(sources), "target": target})

    return {"rules": normalized_rules, "separator": separator}


def schema_field_names(
    schema: Any, origin: str, code: str = "forward_binding_invalid"
) -> frozenset[str]:
    """Extract the field names an ``input_schema`` declares (T-247 C3/D-09).

    ``schema`` is the PARSED native dict (the caller has already
    json.loads'd the DB text and rejected None). Both canonical
    ``CapabilityInputSchema.from_dict`` forms are accepted — nested
    ``{"fields": {name: spec}}`` and flat ``{name: spec}``
    (models/capability.py:83) — plus list-valued field sets handled by
    ``_iter_raw_fields`` (models/capability.py:71-76, entries keyed by an
    explicit ``name`` attr or their position). Minimal detail-grad
    (design D-06): ONLY names are extracted; ``type``/``required``
    entry contents are not additionally validated (from_dict supplies
    defaults, models/capability.py:89-90). Failure classes:
    non-dict schema → ForwardRoutingError(code, 422, f"{origin}: …");
    from_dict raising (a scalar field spec crashes ``raw.get``,
    models/capability.py:86-95 — AttributeError/TypeError/JSONDecodeError
    class) → wrapped to the SAME (code, 422) with the origin named — loud,
    never a 500. ``origin`` is a human label used verbatim in the message
    (e.g. "pin node 'P' capability 'agent.ai'", "slot 'plan' of node 'S'",
    "referenced slot 'plan'"). Contracts: design.md §4/§6.
    """
    if not isinstance(schema, dict):
        raise ForwardRoutingError(
            code,
            422,
            f"{origin}: input_schema must be an object, got"
            f" {type(schema).__name__}",
        )
    try:
        parsed = CapabilityInputSchema.from_dict(schema)
    except Exception as exc:  # deliberate D-07 wrap → loud 422 with the origin
        raise ForwardRoutingError(
            code,
            422,
            f"{origin}: unparseable input_schema: {exc}",
        ) from exc
    return frozenset(parsed.fields.keys())


def apply_binding(
    payload: Mapping[str, Any],
    rules: List[Mapping[str, Any]],  # noqa: UP006  (frozen signature: List)
    separator: str = DEFAULT_BINDING_SEPARATOR,
) -> dict[str, Any]:
    """Apply binding rules to a stage payload; returns the rewritten dict (T-247 C4).

    Rules are applied IN ORDER over a working copy of ``payload``:

    1. A rule "fires" when ≥1 of its ``sources`` is a key of the working
       copy. With one present source the value is COPIED verbatim (native
       type, no stringify); with several present sources the values are
       joined — each non-str value is rendered with ``json.dumps`` (object/
       bool round-trip; str values join unquoted) — using ``separator``
       (list order = join order), and written as ``payload[target]``.
    2. A fired rule CONSUMES its matched sources: they are removed from the
       working copy, except a source that is the rule's own ``target``
       (write-then-sweep would otherwise delete the value). A DORMANT rule
       (zero present sources) writes and consumes nothing.
    3. Chaining is allowed: a later rule may read a field an earlier rule
       wrote (targets are disjoint — PUT form, design §3.1); consumption is
       deterministic (later consumption wins over an earlier target write
       in the final projection).
    4. After the last rule the UNBOUND-FIELDS rule applies (plan C2, T-245
       Decision-6 anti-drift): every surviving key must be a written
       ``target`` OR a member of ``KNOWN_FLOW_PROMPT_FIELDS`` (the design
       §10 reserved set); everything else is dropped.

    Pure: the caller assigns the result to ``stage["payload"]``. No
    claim/owner/gate field is touched here (C4: payload-only — the pin
    transfer and capability rewrite are separate assignments in
    resolve_forward_stages, design §6). Contracts: design.md §3/§6.
    """
    working: dict[str, Any] = dict(payload)
    written_targets: set[str] = set()
    for rule in rules:
        # T-252 const-rule: no 'sources' key → write the fixed value,
        # consume nothing (there is nothing to consume). Native typing is
        # preserved (bool stays bool — the sweep projection is verbatim).
        if "sources" not in rule:
            working[rule["target"]] = rule["value"]
            written_targets.add(rule["target"])
            continue
        # The caller passes VALIDATED form rules (design §3.1) — direct
        # access, missing keys are loud at the form boundary.
        sources = list(rule["sources"])
        target = rule["target"]
        present = [s for s in sources if s in working]
        if not present:
            # A DORMANT rule (zero present sources) writes and consumes
            # nothing — but the final projection below still applies
            # ("kein stillem Passthrough", plan Architecture).
            continue
        if len(present) == 1:
            # Copy the field value verbatim (native type, no stringify).
            value = working[present[0]]
        else:
            # Join: str values unquoted, every other value json.dumps'd
            # (object/bool round-trip); list order = join order.
            parts = [
                working[s] if isinstance(working[s], str) else json.dumps(working[s])
                for s in present
            ]
            value = separator.join(parts)
        working[target] = value
        written_targets.add(target)
        # A fired rule CONSUMES its matched sources — except a source that
        # is the rule's own target (write-then-sweep would delete it).
        for s in present:
            if s != target:
                del working[s]
    # Unbound-fields sweep (T-245 Decision-6 anti-drift): written targets
    # and the §10 reserved prompt set survive; everything else flies.
    return {
        key: value
        for key, value in working.items()
        if key in written_targets or key in KNOWN_FLOW_PROMPT_FIELDS
    }


def check_binding_schema(parsed_schema: Any, rules: list[dict[str, Any]]) -> None:
    """Hard PUT check of binding targets against a capability schema (T-252).

    Generic by design (Ronny 2026-10-26): works for EVERY source and EVERY
    target of the capability — the rule set is checked uniformly.
    ``parsed_schema`` is the native parsed ``input_schema`` dict (both
    ``from_dict`` forms — nested ``{"fields": …}`` and flat) or ``None``.

    - The schema is **None** or fieldless → no checks at all (the PUT
      refuses such bindings earlier only when the binding NEEDS a
      schema — T-247 form; here we stay permissive-by-absence).
    - Every rule's ``target`` must name a field in the schema — the same
      name check ``schema_field_names`` supplies (T-247 form, now moved
      to this helper as the single authority for the binding ladder).
    - Const rules (``target`` + ``value``, T-252): when the field spec
      declares a ``type``, the value must match it NATIVELY —
      string→str, boolean→bool (never accept the string "true"),
      integer→int (bool excluded!), number→int/float, null→always ok.
      Spec without a ``type`` (T-247 minimal-detail form, D-06) →
      name-only check.
      Copy/join rules (carry ``sources`` instead) are never type-checked
      here — their runtime value comes from the submit payload, the
      submitters's own validation domain (Consumer-Zwang rejected, D-09).
    Violations raise ``ForwardRoutingError("forward_binding_invalid",
    422, …)``. Callers: the PUT ladder (mapping upsert); the form is
    validated separately by ``validate_binding_form``.
    """
    def _bad(msg: str) -> ForwardRoutingError:
        return ForwardRoutingError("forward_binding_invalid", 422, msg)

    if not parsed_schema:
        return
    fields = parsed_schema.get("fields") if isinstance(parsed_schema, dict) else None
    if not isinstance(fields, dict) or not fields:
        if isinstance(parsed_schema, dict) and parsed_schema:
            fields = parsed_schema  # flat from_dict form
        else:
            return
    for rule in rules:
        target = rule["target"]
        spec = fields.get(target)
        if spec is not None and not isinstance(spec, dict):
            spec = {"name": target}  # list-form field entries degrade to name-only
        if spec is None:
            raise ForwardRoutingError(
                "forward_binding_unknown_target_field",
                422,
                f"binding target '{target}' is not a field of the target"
                " capability schema",
            )
        if "sources" in rule:
            continue  # copy/join: runtime value is the submitter's domain
        if "value" not in rule:
            continue  # dormant const (form-validated: value present) — inert
        value = rule["value"]
        declared = spec.get("type") if isinstance(spec, dict) else None
        if not isinstance(declared, str) or not declared.strip():
            continue  # D-06 minimal-detail: name-only check
        declared = declared.strip()
        if value is None:
            # Strict form (T-252 test contract): an explicit null write
            # against a field that declares a type is a mismatch — the
            # editor never sends null (unchecked boolean = false), so a
            # null here is a hand-written API payload: refuse loud.
            raise _bad(
                f"const value for target '{target}': null does not match"
                f" declared type '{declared}'"
            )
        type_ok = {
            "string": isinstance(value, str) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
        }.get(declared)
        if type_ok is None:
            continue  # unknown declared type: no runtime check (D-06)
        if not type_ok:
            raise _bad(
                f"const value for target '{target}' must match declared type"
                f" '{declared}' (got {type(value).__name__})"
            )


def load_slot_declaration(conn: DBConn, node_id: str, slot: str) -> Optional[str]:  # noqa: UP045  (frozen signature: Optional)
    """Load a declared slot's stored ``slot_type`` (T-245 C5 submit gate).

    Returns the RAW stored value: ``None`` when the node has no
    declaration row for ``slot`` or the row predates migration 16
    (``slot_type IS NULL``). Takes the caller's connection — no second
    SQLite handle inside the validation loop (precedes
    core/discovery.py:327-329 connection-contention rationale). Single
    query shape: ``SELECT slot_type FROM forward_slots WHERE node_id = ?
    AND slot = ?``. Callers treat ``None`` as the ``forward`` default; the
    refuse decision is ``decl_type is not None and decl_type !=
    SLOT_TYPE_FORWARD``. Contracts: design.md §7.
    """
    row = conn.execute(
        q(
            "SELECT slot_type FROM forward_slots WHERE node_id = ? AND slot = ?",
            (node_id, slot),
        )
    ).fetchone()
    if row is None:
        return None
    return row["slot_type"]


def resolve_forward_stages(
    submitter_node_id: str,
    stages: List[dict[str, Any]],  # noqa: UP006  (frozen signature: List)
) -> None:
    """Rewrite every forward stage in place to its mapped target capability.

    All-or-nothing: validates every forward stage before mutating any;
    raises ``ForwardRoutingError`` on the first failure (submitter unknown →
    404 ``forward_node_unknown``; unmapped slot → 422
    ``forward_mapping_missing``; malformed slot → 422
    ``forward_mapping_invalid``; slot declared as a value slot (T-245) →
    422 ``forward_slot_not_routable`` — the type gate runs BEFORE mapping
    resolution, because a text/bool slot has no mapping by definition and
    the error must name the real cause, design.md §7). On success each
    forward stage gets ``stage["capability"] = target``,
    ``stage["forward_slot"] = slot`` and the ``forward_slot`` key is removed
    from its payload. T-246: a mapping pin (``target_node_id``) travels with
    the resolution — the rewrite pass sets ``stage["target_node_id"]``
    (None = unpinned) next to ``capability``/``forward_slot``; the T-245 type
    gate still runs BEFORE the mapping SELECT, so a value-declared slot
    refuses with ``forward_slot_not_routable`` regardless of any pin.
    T-247: a mapping's stored ``binding`` (Ronny form: rules + separator)
    is parsed and form-validated in the VALIDATION pass — corrupt/
    shape-broken rows refuse the submit with 422 ``forward_binding_invalid``
    (never a silent passthrough, never a 500), and the rewrite pass
    projects the payload (copy/join per rules; unbound non-reserved fields
    dropped; the ``KNOWN_FLOW_PROMPT_FIELDS`` reserved set survives —
    design.md §10). No schema re-check at submit (Consumer-Zwang rejected,
    D-09); claim gates/owner semantics are untouched (payload-only).
    Read-only SELECTs —
    the task write happens in ``Scheduler.create_task``. Contracts:
    design.md §3.1.c + §7.
    """
    if not any(is_forward_stage(s) for s in stages):
        return

    conn = get_conn()
    try:
        row = conn.execute(
            q("SELECT 1 FROM nodes WHERE node_id = ?", (submitter_node_id,))
        ).fetchone()
        if not row:
            raise ForwardRoutingError(
                "forward_node_unknown",
                404,
                f"submitter node '{submitter_node_id}' is not registered",
            )

        # ── T-253: the submitter's DECLARED VALUE slots (slot_type
        # text/bool with a stored value) ride as payload DEFAULTS for
        # every forward stage of the SAME submitter. ``ref_slot`` names
        # the routing slot the default belongs to (``ref_field`` the
        # payload field; default = the value slot's own name). Payload
        # wins: a field the runner actually submits is NEVER
        # overwritten. Booleans arrive natively (JSON text at storage,
        # parsed here); the writer's "null" text = no default.
        value_defaults: dict[str, dict[str, Any]] = {}
        vrows = conn.execute(
            q(
                "SELECT slot, value, ref_slot, ref_field FROM forward_slots"
                " WHERE node_id = ? AND slot_type != 'forward'"
                " AND value IS NOT NULL",
                (submitter_node_id,),
            )
        ).fetchall()
        for vrow in vrows:
            raw = vrow["value"]
            value = None
            if raw is not None and raw != "null":
                try:
                    value = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    value = str(raw)  # the T-245 writer's fallback form
            if value is None:
                continue
            field_name = vrow["ref_field"] or vrow["slot"]
            forms = value_defaults.setdefault(vrow["ref_slot"] or "*", {})
            forms[field_name] = value
        for _k in list(value_defaults):
            if value_defaults[_k] == {}:
                del value_defaults[_k]

        # Validation pass — resolve EVERY forward stage before any mutation.
        resolved: List[dict[str, Any]] = []  # noqa: UP006  (frozen signature: List)
        for stage in stages:
            if not is_forward_stage(stage):
                continue
            payload = stage.get("payload") or {}
            if not isinstance(payload, dict):
                raise ForwardRoutingError(
                    "forward_mapping_invalid",
                    422,
                    f"payload of forward stage '{stage.get('stage_name')}'"
                    " must be an object",
                )
            slot = normalize_slot(payload.get("forward_slot"))
            # ── T-245 gate: a slot the node declared as a value slot
            # (text/bool) is not routable. Checked BEFORE mapping resolution —
            # a value slot has no mapping by definition, so the refusal must
            # name the declared type as the real cause (design.md §7). NULL /
            # absent declarations fall through as the forward default.
            decl_type = load_slot_declaration(conn, submitter_node_id, slot)
            if decl_type is not None and decl_type != SLOT_TYPE_FORWARD:
                raise ForwardRoutingError(
                    "forward_slot_not_routable",
                    422,
                    f"slot '{slot}' of node '{submitter_node_id}' is declared with"
                    f" type '{decl_type}' — value slots are not routable",
                )
            mrow = conn.execute(
                q(
                    "SELECT target_capability, target_node_id, binding"
                    " FROM forward_mappings"
                    " WHERE node_id = ? AND slot = ?",
                    (submitter_node_id, slot),
                )
            ).fetchone()
            if not mrow:
                raise ForwardRoutingError(
                    "forward_mapping_missing",
                    422,
                    f"no mapping for node '{submitter_node_id}' slot '{slot}'",
                )
            target = mrow["target_capability"]
            # Defensive loop guard — mapping rows can never carry it, but
            # read old data politely.
            if target == FORWARD_CAPABILITY:
                raise ForwardRoutingError(
                    "forward_mapping_invalid",
                    422,
                    "mapping points at the forward pseudo-capability",
                )
            # T-247: parse + form-validate the stored binding in the VALIDATION
            # pass (all-or-nothing); corrupt/shape-broken rows refuse the submit
            # LOUD (forward_binding_invalid) — never a silent passthrough, never a
            # 500 (D-07: the guarded-parse class here overrides the unguarded
            # legacy idiom intentionally; the message names node+slot).
            raw_binding = mrow["binding"]
            if raw_binding is None:
                bound_form = None
            else:
                try:
                    bound_form = validate_binding_form(json.loads(raw_binding))
                except json.JSONDecodeError as exc:
                    raise ForwardRoutingError(
                        "forward_binding_invalid",
                        422,
                        f"corrupt binding JSON on mapping (node"
                        f" '{submitter_node_id}', slot '{slot}'): {exc}",
                    ) from exc
            resolved.append(
                {
                    "stage": stage,
                    "slot": slot,
                    "target": target,
                    "pin": mrow["target_node_id"],
                    "binding": bound_form,
                }
            )
    finally:
        conn.close()

    # T-253 (Ronny's original slot-vision, deferred from T-247 D-15):
    # collected INSIDE the conn scope — see the defaults build above the
    # validation loop. (The rewrite pass uses only the collected dict.)
    for entry in resolved:
        stage = entry["stage"]
        stage["capability"] = entry["target"]
        stage["forward_slot"] = entry["slot"]
        # T-246: execution address copied from the mapping pin — set ALWAYS
        # (None = unpinned, deterministic over absence-of-key, design D-08).
        stage["target_node_id"] = entry["pin"]
        payload = stage.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        # The claiming node receives a payload shaped for the real target —
        # slot provenance lives on the stage column, not in the payload.
        payload.pop("forward_slot", None)
        # T-253: value-slot defaults inject BEFORE the binding —
        # ``setdefault`` form (the submitted payload WINS over every
        # default; the binding then projects exactly what it is told).
        form_key = entry["slot"]
        for def_name, def_value in (value_defaults.get(form_key) or {}).items():
            payload.setdefault(def_name, def_value)
        for def_name, def_value in (value_defaults.get("*") or {}).items():
            payload.setdefault(def_name, def_value)
        # T-247: binding projects the payload (Copy/join per rules,
        # unbound fields fly except the reserved prompt fields, §3.3/
        # §10). Payload-only — capability/pin/claim fields are already
        # set and are NOT touched here (C4).
        if entry["binding"] is not None:
            payload = apply_binding(
                payload,
                entry["binding"]["rules"],
                entry["binding"]["separator"],
            )
        stage["payload"] = payload  # still a dict here; JSON-string at storage


def list_forward_mappings(node_id: Optional[str] = None) -> list[dict[str, Any]]:  # noqa: UP045  (frozen signature)
    """List mappings (all, or filtered by ``node_id``).

    Rows carry exactly ``node_id, slot, target_capability, target_node_id,
    binding, created_at, updated_at`` (no internal ``mapping_id``; ``binding``
    = parsed native dict or None — writer-validated parse — the upsert is
    the only writer), ordered by node_id, slot. Unknown ``node_id`` → 404
    ``forward_node_unknown``. Contracts:
    design.md §3.1.d + T-246 design §3.3.
    """
    conn = get_conn()
    try:
        if node_id is not None:
            row = conn.execute(
                q("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,))
            ).fetchone()
            if not row:
                raise ForwardRoutingError(
                    "forward_node_unknown",
                    404,
                    f"submitter node '{node_id}' is not registered",
                )
            rows = conn.execute(
                q(
                    "SELECT node_id, slot, target_capability, target_node_id,"
                    " binding, created_at, updated_at"
                    " FROM forward_mappings WHERE node_id = ?"
                    " ORDER BY node_id ASC, slot ASC",
                    (node_id,),
                )
            ).fetchall()
        else:
            rows = conn.execute(
                q(
                    "SELECT node_id, slot, target_capability, target_node_id,"
                    " binding, created_at, updated_at"
                    " FROM forward_mappings ORDER BY node_id ASC, slot ASC"
                )
            ).fetchall()
        # D-06 readback: only the validating upsert writes the TEXT — the
        # parse is unguarded; a hand-corrupted row fails loud (design §2.3).
        out: List[dict[str, Any]] = []  # noqa: UP006  (frozen signature: List)
        for r in rows:
            out_row = dict(r)
            out_row["binding"] = (
                json.loads(out_row["binding"])
                if out_row["binding"] is not None
                else None
            )
            out.append(out_row)
        return out
    finally:
        conn.close()


def replace_forward_slots(node_id: str, slots: Any) -> int:
    """Replace the node's declared forward slots (T-Forward-2, typed in T-245).

    ``slots`` is a list of dicts (``slot`` + optional ``label`` /
    ``description`` and, since T-245, the typed fields ``type`` / ``value`` /
    ``ref_slot`` / ``ref_field`` / ``input_schema``) as the node heartbeats
    them from ``node.yaml``. Validation is two-phase and in-memory (T-245
    design.md §4): phase A parses/validates EVERY entry — including the
    ref-existence/direction check against the in-memory set of the whole
    declaration — and only a fully valid declaration reaches the
    DELETE/INSERT write, so a rejected declaration leaves the previous rows
    intact (the heartbeat stays 200 via core/discovery.py swallow-and-log).
    T-247 (D-09 fulfillment): a declared ref is ALSO content-checked in
    Phase A2 — ``ref_field`` must be a field of the referenced slot's
    ``input_schema`` (schema-gated refs: a referenced slot without
    ``input_schema`` → 422 ``forward_slot_invalid``; unparseable referenced
    schema → 422 ditto, origin named).
    An absent ``type`` resolves to ``forward`` and is persisted resolved
    (never NULL, D-08); ``value`` and ``input_schema`` are persisted as
    ``json.dumps`` strings (read back as native JSON by ``list_forward_slots``).
    Unknown node → 404; malformed rows are rejected with 422 — the
    declaration is node-owned metadata, not admin config, so errors are
    loud (operator must fix node.yaml) instead of silently dropping rows.
    Returns the number of stored rows (0 for an empty declaration).
    """
    if not isinstance(slots, list):
        raise ForwardRoutingError(
            "forward_slot_invalid",
            422,
            f"forward_slots must be a list, got {type(slots).__name__}",
        )

    # ── Phase A — parse & validate every entry in memory, before any DB
    # write: a rejected declaration must never touch the existing rows.
    parsed: list[dict[str, Any]] = []
    for entry in slots:
        if not isinstance(entry, dict):
            raise ForwardRoutingError(
                "forward_slot_invalid",
                422,
                f"forward_slots entries must be objects, got {type(entry).__name__}",
            )
        slot = normalize_slot(entry.get("slot"))
        slot_type = normalize_slot_type(entry.get("type"))
        value = normalize_slot_value(slot_type, entry.get("value"))
        ref_slot, ref_field = normalize_slot_ref(entry, slot_type) or (None, None)
        raw_schema = entry.get("input_schema")
        if raw_schema is not None:
            if slot_type != SLOT_TYPE_FORWARD:
                raise ForwardRoutingError(
                    "forward_slot_invalid",
                    422,
                    f"slot '{slot}': input_schema is only allowed on forward slots",
                )
            # Structure-only validation (D-05): dict-or-422, content of the
            # schema is NOT validated here (field contract is T-247, D-09).
            if not isinstance(raw_schema, dict):
                raise ForwardRoutingError(
                    "forward_slot_invalid",
                    422,
                    f"slot '{slot}': input_schema must be an object,"
                    f" got {type(raw_schema).__name__}",
                )
        label = entry.get("label") if isinstance(entry.get("label"), str) else None
        description = entry.get("description") if isinstance(entry.get("description"), str) else None
        parsed.append(
            {
                "slot": slot,
                "label": (label or "").strip() or None,
                "description": (description or "").strip() or None,
                "slot_type": slot_type,
                "value": value,
                "ref_slot": ref_slot,
                "ref_field": ref_field,
                "input_schema": raw_schema,
            }
        )

    # ── Phase A2 — ref existence + direction against the declaration itself
    # (in-memory, declaration order irrelevant, duplicate slot names resolve
    # last-wins like the upsert below). A ref must target a forward slot;
    # cycles are structurally impossible (value slots are not referable).
    type_map: dict[str, str] = {row["slot"]: row["slot_type"] for row in parsed}
    schema_map: dict[str, Any] = {row["slot"]: row["input_schema"] for row in parsed}
    for row in parsed:
        if row["ref_slot"] is None:
            continue
        if type_map.get(row["ref_slot"]) != SLOT_TYPE_FORWARD:
            raise ForwardRoutingError(
                "forward_slot_invalid",
                422,
                f"slot '{row['slot']}': ref_slot '{row['ref_slot']}' must"
                " reference a forward slot declared in the same request",
            )
        # T-247 (D-09 fulfillment): a declared ref is content-checked —
        # schema-gated (a referenced slot WITHOUT input_schema is refused)
        # and the ref_field must be one of the referenced slot's declared
        # field names. Unparseable schema → the same loud wrap (origin
        # named). Heartbeats stay 200 via the swallow-and-log caller; the
        # two-phase order keeps the previous valid rows intact.
        referenced = schema_map.get(row["ref_slot"])
        if referenced is None:
            raise ForwardRoutingError(
                "forward_slot_invalid",
                422,
                f"slot '{row['slot']}': ref_field '{row['ref_field']}'"
                f" requires the referenced slot '{row['ref_slot']}'"
                " to declare an input_schema",
            )
        names = schema_field_names(
            referenced,
            f"referenced slot '{row['ref_slot']}'",
            code="forward_slot_invalid",
        )
        if row["ref_field"] not in names:
            raise ForwardRoutingError(
                "forward_slot_invalid",
                422,
                f"slot '{row['slot']}': ref_field '{row['ref_field']}'"
                f" is not a field of slot '{row['ref_slot']}'" + "'s input_schema",
            )

    # ── Phase B — persistence, only after full validation.
    conn = get_conn()
    try:
        row = conn.execute(q("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,))).fetchone()
        if not row:
            raise ForwardRoutingError(
                "forward_node_unknown",
                404,
                f"submitter node '{node_id}' is not registered",
            )
        conn.execute(q("DELETE FROM forward_slots WHERE node_id = ?", (node_id,)))
        if not parsed:
            conn.commit()
            return 0
        now = datetime.now(UTC).isoformat()
        for row in parsed:
            conn.execute(
                q(
                    "INSERT INTO forward_slots (node_id, slot, label, description,"
                    " slot_type, value, ref_slot, ref_field, input_schema, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(node_id, slot) DO UPDATE SET"
                    " label = excluded.label, description = excluded.description,"
                    " slot_type = excluded.slot_type, value = excluded.value,"
                    " ref_slot = excluded.ref_slot, ref_field = excluded.ref_field,"
                    " input_schema = excluded.input_schema,"
                    " updated_at = excluded.updated_at",
                    (
                        node_id,
                        row["slot"],
                        row["label"],
                        row["description"],
                        row["slot_type"],
                        json.dumps(row["value"]),
                        row["ref_slot"],
                        row["ref_field"],
                        json.dumps(row["input_schema"]),
                        now,
                    ),
                )
            )
        conn.commit()
        return len(parsed)
    finally:
        conn.close()


def list_forward_slots(node_id: Optional[str]) -> list[dict[str, Any]]:  # noqa: UP045  (frozen signature)
    """List declared forward slots (all, or filtered by ``node_id``).

    Rows carry exactly ``node_id, slot, label, description, slot_type,
    value, ref_slot, ref_field, input_schema, updated_at`` (T-245). A NULL
    ``slot_type`` (rows predating migration 16) is resolved to
    ``"forward"``; ``value`` and ``input_schema`` are returned as parsed
    native JSON values — they are stored as ``json.dumps`` strings, so the
    parse is intentionally unguarded (D-06: only the phase-A-validating
    writer produces them; a corrupt row raises loudly instead of leaking a
    stale string). Unknown ``node_id`` → 404
    ``forward_node_unknown``. These are node-declared
    metadata rows only — the target capability lives in ``forward_mappings``.
    """
    conn = get_conn()
    try:
        if node_id is not None:
            row = conn.execute(q("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,))).fetchone()
            if not row:
                raise ForwardRoutingError(
                    "forward_node_unknown",
                    404,
                    f"submitter node '{node_id}' is not registered",
                )
            rows = conn.execute(
                q(
                    "SELECT node_id, slot, label, description, slot_type, value,"
                    " ref_slot, ref_field, input_schema, updated_at"
                    " FROM forward_slots WHERE node_id = ?"
                    " ORDER BY node_id ASC, slot ASC",
                    (node_id,),
                )
            ).fetchall()
        else:
            rows = conn.execute(
                q(
                    "SELECT node_id, slot, label, description, slot_type, value,"
                    " ref_slot, ref_field, input_schema, updated_at"
                    " FROM forward_slots ORDER BY node_id ASC, slot ASC"
                )
            ).fetchall()
        # Pre-migration rows keep NULL in the typed columns; the API resolves
        # them so callers never see a bare NULL slot_type (D-08 read side).
        out: List[dict[str, Any]] = []  # noqa: UP006  (frozen signature: List)
        for r in rows:
            raw_value = r["value"]
            raw_schema = r["input_schema"]
            out.append(
                {
                    "node_id": r["node_id"],
                    "slot": r["slot"],
                    "label": r["label"],
                    "description": r["description"],
                    "slot_type": (
                        r["slot_type"]
                        if r["slot_type"] is not None
                        else DEFAULT_SLOT_TYPE
                    ),
                    "value": json.loads(raw_value) if raw_value is not None else None,
                    "ref_slot": r["ref_slot"],
                    "ref_field": r["ref_field"],
                    "input_schema": (
                        json.loads(raw_schema) if raw_schema is not None else None
                    ),
                    "updated_at": r["updated_at"],
                }
            )
        return out
    finally:
        conn.close()


def upsert_forward_mapping(
    node_id: str,
    slot: Any,
    target_capability: Any,
    target_node_id: Any = None,
    binding: Any = None,
) -> dict[str, Any]:
    """Create or update a (node, slot) mapping after full validation.

    Validation ladder (design.md §4, T-247): slot normalization (422
    forward_mapping_invalid) → target strip/shape (422
    forward_mapping_invalid) → target == forward (400 redirect_self) →
    submitter node existence (404 forward_node_unknown) → pin form
    (422 target_node_invalid) → pin path (422 target_node_unknown /
    target_node_lacks_capability) → unpinned aggregate check (422
    target_unknown_capability) — steps 1-7 byte-identical to T-246 —
    then T-247 steps 8-9 (design.md §4): binding FORM validation
    (validate_binding_form; None/omitted = legacy passthrough, full-row
    replace like the pin; 422 forward_binding_invalid) and binding FIELD
    checks on the same conn — SOURCES against the submitter's
    forward_slots.input_schema when the slot declares one (422
    forward_binding_unknown_source_field; undeclared = free names, D-02);
    TARGETS against the pin node's capability row input_schema
    (``SELECT input_schema FROM node_capabilities WHERE node_id = ? AND
    capability_name = ?`` — the exact T-246 row query, never the aggregate,
    D-03) on the pin path, resp. the slot's input_schema on the unpinned
    path (plan C3; 422 forward_binding_unknown_target_field when the
    schema lacks the target, 422 forward_binding_requires_schema when no
    usable schema exists in the branch). The binding is stored as
    ``json.dumps`` of the normalized form; the readback carries it as the
    parsed native dict (or None). Returns the post-write row (list keys
    incl. ``target_node_id`` + ``binding``).
    """
    slot = normalize_slot(slot)

    if not isinstance(target_capability, str):
        raise ForwardRoutingError(
            "forward_mapping_invalid",
            422,
            f"target capability must be a string, got {type(target_capability).__name__}",
        )
    target = target_capability.strip()
    if not target:
        raise ForwardRoutingError(
            "forward_mapping_invalid",
            422,
            "target capability must not be empty",
        )
    if len(target) > 255:
        raise ForwardRoutingError(
            "forward_mapping_invalid",
            422,
            "target capability exceeds the 255 character cap",
        )
    if target == FORWARD_CAPABILITY:
        raise ForwardRoutingError(
            "redirect_self",
            400,
            f"mapping target for node '{node_id}' slot '{slot}' may not be 'forward'",
        )

    conn = get_conn()
    try:
        row = conn.execute(
            q("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,))
        ).fetchone()
        if not row:
            raise ForwardRoutingError(
                "forward_node_unknown",
                404,
                f"submitter node '{node_id}' is not registered",
            )

        # ── T-246 pin form (design §3.2 step 5, D-01): string-only + 64 cap;
        # a bool never coerces (same guard class as normalize_slot), ""
        # strips to the explicit unpin form. None = field omitted = unpin
        # (D-02 full-row replace — the guards only bind when a pin is sent).
        # No mint-regex: a malformed id can never exist in nodes, so
        # existence IS the validator.
        if target_node_id is not None:
            if not isinstance(target_node_id, str):
                raise ForwardRoutingError(
                    "target_node_invalid",
                    422,
                    f"target_node_id must be a string, got {type(target_node_id).__name__}",
                )
            if len(target_node_id) > 64:
                raise ForwardRoutingError(
                    "target_node_invalid",
                    422,
                    "target_node_id exceeds the 64 character cap",
                )
        pin = (target_node_id or "").strip() or None

        if pin is not None:
            # ── T-246 pin path (design §3.2 step 6, D-03): the pin node must
            # exist and must advertise the mapping's target capability ITSELF
            # (node-scoped row query — never the orphan-vulnerable aggregate;
            # design §10). The aggregate catalog step is SKIPPED on this path.
            pin_row = conn.execute(
                q("SELECT 1 FROM nodes WHERE node_id = ?", (pin,))
            ).fetchone()
            if not pin_row:
                raise ForwardRoutingError(
                    "target_node_unknown",
                    422,
                    f"pin target node '{pin}' is not registered",
                )
            advertises = conn.execute(
                q(
                    "SELECT 1 FROM node_capabilities"
                    " WHERE node_id = ? AND capability_name = ?",
                    (pin, target),
                )
            ).fetchone()
            if not advertises:
                raise ForwardRoutingError(
                    "target_node_lacks_capability",
                    422,
                    f"pin target node '{pin}' does not advertise"
                    f" capability '{target}'",
                )
        else:
            catalog = conn.execute(
                q(
                    "SELECT 1 FROM node_capabilities WHERE capability_name = ? LIMIT 1",
                    (target,),
                )
            ).fetchone()
            if not catalog:
                raise ForwardRoutingError(
                    "target_unknown_capability",
                    422,
                    f"target capability '{target}' is not advertised by any node",
                )

        # ── T-247 step 8 — binding FORM validation (design.md §4, ladder
        # step 8): pure/DB-free, runs on both pin/unpinned paths. None /
        # omitted = legacy passthrough — no check, full-row replace like
        # the pin (D-05). Transport tolerance: the endpoint passes the
        # parsed pydantic ``binding``; ``model_dump()`` is exactly the
        # canonical dict form (models BindingRules, test 32) — the form
        # authority below stays dict-only (design §3.1).
        form = None
        if binding is not None:
            if not isinstance(binding, dict) and hasattr(binding, "model_dump"):
                binding = binding.model_dump()
            form = validate_binding_form(binding)

        # ── T-247 step 9 — binding FIELD checks (design.md §4.2 branch
        # table), on the SAME conn as steps 4-7. SOURCES validate against
        # the submitter's slot declaration ONLY when it declares an
        # input_schema (D-02); TARGETS are hard in both branches.
        if form is not None:
            slot_row = conn.execute(
                q(
                    "SELECT input_schema FROM forward_slots"
                    " WHERE node_id = ? AND slot = ?",
                    (node_id, slot),
                )
            ).fetchone()
            raw_slot_schema = slot_row["input_schema"] if slot_row is not None else None
            slot_names: frozenset[str] | None = None
            slot_schema: Any = None  # T-252: pre-bound — pyright-clean; the
            # unpinned branch only reads it when slot_names proves the schema was parsed
            if raw_slot_schema is not None:
                try:
                    slot_schema = json.loads(raw_slot_schema)
                except json.JSONDecodeError as exc:
                    raise ForwardRoutingError(
                        "forward_binding_invalid",
                        422,
                        f"slot '{slot}' of node '{node_id}': corrupt"
                        f" input_schema JSON: {exc}",
                    ) from exc
                # A slot that declares NO schema stores json.dumps(None) =
                # "null" (T-245 writer) — semantically the NULL schema:
                # sources pass unvalidated, the targets branch decides
                # requires_schema (the caller rejects None — design §3.2).
                if slot_schema is not None:
                    slot_names = schema_field_names(
                        slot_schema, f"slot '{slot}' of node '{node_id}'"
                    )
            for rule in form["rules"]:
                if "sources" not in rule:
                    continue  # T-252 const-rule: consumes nothing, sources-free
                if slot_names is not None:
                    for source_name in rule["sources"]:
                        if source_name not in slot_names:
                            raise ForwardRoutingError(
                                "forward_binding_unknown_source_field",
                                422,
                                f"slot '{slot}' of node '{node_id}':"
                                f" binding source '{source_name}' is not a"
                                " field of the slot's input_schema",
                            )
            if pin is not None:
                # Pinned targets = the EXACT (pin, target_capability) row's
                # input_schema (D-03) — never the aggregate. Two distinct
                # absences, two codes: no contract row → requires_schema;
                # a present-but-∅ schema → unknown_target_field.
                cap_row = conn.execute(
                    q(
                        "SELECT input_schema FROM node_capabilities"
                        " WHERE node_id = ? AND capability_name = ?",
                        (pin, target),
                    )
                ).fetchone()
                if cap_row is None or cap_row["input_schema"] is None:
                    raise ForwardRoutingError(
                        "forward_binding_requires_schema",
                        422,
                        f"pin node '{pin}' capability '{target}': no"
                        " input_schema declared — a binding requires the"
                        " provider's declared contract",
                    )
                try:
                    pin_schema = json.loads(cap_row["input_schema"])
                except (json.JSONDecodeError, TypeError) as exc:
                    raise ForwardRoutingError(
                        "forward_binding_invalid",
                        422,
                        f"pin node '{pin}' capability '{target}': corrupt"
                        f" input_schema JSON: {exc}",
                    ) from exc
                # T-252: name + (const-)value-type checks against the pin
                # node's schema — single authority, generic over every
                # source and every target field.
                check_binding_schema(pin_schema, form["rules"])
            else:
                # Unpinned: the SAME slot schema as the sources step (plan
                # C3 — never the orphan-vulnerable aggregate, D-04). The
                # binding path is only allowed WITH a declared schema.
                if slot_names is None:
                    raise ForwardRoutingError(
                        "forward_binding_requires_schema",
                        422,
                        f"slot '{slot}' of node '{node_id}': no input_schema"
                        " declared — an unpinned binding requires the"
                        " slot's declared contract",
                    )
                # T-252: name + (const-)value-type checks against the slot
                # schema — check_binding_schema raises the same code for
                # unknown targets on this branch.
                check_binding_schema(
                    slot_schema, form["rules"] if slot_names is not None else []
                )

        now = datetime.now(UTC).isoformat()
        existing = conn.execute(
            q(
                "SELECT mapping_id, created_at FROM forward_mappings"
                " WHERE node_id = ? AND slot = ?",
                (node_id, slot),
            )
        ).fetchone()
        if existing is None:
            mapping_id = f"fwdm_{secrets.token_urlsafe(12)}"
            try:
                conn.execute(
                    q(
                        "INSERT INTO forward_mappings"
                        " (mapping_id, node_id, slot, target_capability,"
                        " target_node_id, binding, created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            mapping_id,
                            node_id,
                            slot,
                            target,
                            pin,
                            json.dumps(form) if form is not None else None,
                            now,
                            now,
                        ),
                    )
                )
            except (sqlite3.IntegrityError, sa_exc.IntegrityError):
                # Lost a race against a concurrent upsert of the same
                # (node, slot) — the UNIQUE constraint is the backstop;
                # re-read and UPDATE the winner row instead.
                conn.rollback()
                existing = conn.execute(
                    q(
                        "SELECT mapping_id, created_at FROM forward_mappings"
                        " WHERE node_id = ? AND slot = ?",
                        (node_id, slot),
                    )
                ).fetchone()
                if existing is None:
                    raise
        if existing is not None:
            conn.execute(
                q(
                    "UPDATE forward_mappings SET target_capability = ?,"
                    " target_node_id = ?, binding = ?, updated_at = ?"
                    " WHERE mapping_id = ?",
                    (
                        target,
                        pin,
                        json.dumps(form) if form is not None else None,
                        now,
                        existing["mapping_id"],
                    ),
                )
            )
        row = conn.execute(
            q(
                "SELECT node_id, slot, target_capability, target_node_id,"
                " binding, created_at, updated_at"
                " FROM forward_mappings WHERE node_id = ? AND slot = ?",
                (node_id, slot),
            )
        ).fetchone()
        conn.commit()
        # D-06 readback: the validating upsert is the only writer of this
        # TEXT — unguarded parse, a corrupt row fails loud (design §2.3).
        out = dict(row)
        out["binding"] = (
            json.loads(out["binding"]) if out["binding"] is not None else None
        )
        return out
    finally:
        conn.close()


def delete_forward_mapping(node_id: str, slot: str) -> None:
    """Delete a mapping; 404 ``forward_mapping_missing`` when absent."""
    slot = normalize_slot(slot)
    conn = get_conn()
    try:
        cur = conn.execute(
            q(
                "DELETE FROM forward_mappings WHERE node_id = ? AND slot = ?",
                (node_id, slot),
            )
        )
        if cur.rowcount == 0:
            raise ForwardRoutingError(
                "forward_mapping_missing",
                404,
                f"no mapping for node '{node_id}' slot '{slot}'",
            )
        conn.commit()
    finally:
        conn.close()