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
)

from relay_server.core.db import get_conn, q
from sqlalchemy import exc as sa_exc

FORWARD_CAPABILITY: Final[str] = "forward"
SLOT_MAX_CHARS: Final[int] = 64
_SLOT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


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


def resolve_forward_stages(
    submitter_node_id: str,
    stages: List[dict[str, Any]],  # noqa: UP006  (frozen signature: List)
) -> None:
    """Rewrite every forward stage in place to its mapped target capability.

    All-or-nothing: validates every forward stage before mutating any;
    raises ``ForwardRoutingError`` on the first failure (submitter unknown →
    404 ``forward_node_unknown``; unmapped slot → 422
    ``forward_mapping_missing``; malformed slot → 422
    ``forward_mapping_invalid``). On success each forward stage gets
    ``stage["capability"] = target``, ``stage["forward_slot"] = slot`` and
    the ``forward_slot`` key is removed from its payload. Read-only SELECTs —
    the task write happens in ``Scheduler.create_task``. Contracts:
    design.md §3.1.c.
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
            mrow = conn.execute(
                q(
                    "SELECT target_capability FROM forward_mappings"
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
            resolved.append({"stage": stage, "slot": slot, "target": target})
    finally:
        conn.close()

    # Rewrite pass — only after full validation (§3.1.c step 4/5).
    for entry in resolved:
        stage = entry["stage"]
        stage["capability"] = entry["target"]
        stage["forward_slot"] = entry["slot"]
        payload = stage.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        # The claiming node receives a payload shaped for the real target —
        # slot provenance lives on the stage column, not in the payload.
        payload.pop("forward_slot", None)
        stage["payload"] = payload


def list_forward_mappings(node_id: Optional[str] = None) -> list[dict[str, Any]]:  # noqa: UP045  (frozen signature)
    """List mappings (all, or filtered by ``node_id``).

    Rows carry exactly ``node_id, slot, target_capability, created_at,
    updated_at`` (no internal ``mapping_id``), ordered by node_id, slot.
    Unknown ``node_id`` → 404 ``forward_node_unknown``. Contracts:
    design.md §3.1.d.
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
                    "SELECT node_id, slot, target_capability, created_at, updated_at"
                    " FROM forward_mappings WHERE node_id = ?"
                    " ORDER BY node_id ASC, slot ASC",
                    (node_id,),
                )
            ).fetchall()
        else:
            rows = conn.execute(
                q(
                    "SELECT node_id, slot, target_capability, created_at, updated_at"
                    " FROM forward_mappings ORDER BY node_id ASC, slot ASC"
                )
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def replace_forward_slots(node_id: str, slots: Any) -> int:
    """Replace the node's declared forward slots (T-Forward-2).

    ``slots`` is a list of dicts (``slot`` + optional ``label`` /
    ``description``) as the node heartbeats them from ``node.yaml``.
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

    parsed: list[tuple[str, Optional[str], Optional[str]]] = []  # noqa: UP006  (frozen signature: List)
    for entry in slots:
        if not isinstance(entry, dict):
            raise ForwardRoutingError(
                "forward_slot_invalid",
                422,
                f"forward_slots entries must be objects, got {type(entry).__name__}",
            )
        slot = normalize_slot(entry.get("slot"))
        label = entry.get("label") if isinstance(entry.get("label"), str) else None
        description = entry.get("description") if isinstance(entry.get("description"), str) else None
        parsed.append((slot, (label or "").strip() or None, (description or "").strip() or None))

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
        for slot, label, description in parsed:
            conn.execute(
                q(
                    "INSERT INTO forward_slots (node_id, slot, label, description, updated_at)"
                    " VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(node_id, slot) DO UPDATE SET"
                    " label = excluded.label, description = excluded.description,"
                    " updated_at = excluded.updated_at",
                    (node_id, slot, label, description, now),
                )
            )
        conn.commit()
        return len(parsed)
    finally:
        conn.close()


def list_forward_slots(node_id: Optional[str]) -> list[dict[str, Any]]:  # noqa: UP045  (frozen signature)
    """List declared forward slots (all, or filtered by ``node_id``).

    Rows carry ``node_id, slot, label, description, updated_at``. Unknown
    ``node_id`` → 404 ``forward_node_unknown``. These are node-declared
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
                    "SELECT node_id, slot, label, description, updated_at"
                    " FROM forward_slots WHERE node_id = ?"
                    " ORDER BY node_id ASC, slot ASC",
                    (node_id,),
                )
            ).fetchall()
        else:
            rows = conn.execute(
                q(
                    "SELECT node_id, slot, label, description, updated_at"
                    " FROM forward_slots ORDER BY node_id ASC, slot ASC"
                )
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_forward_mapping(
    node_id: str,
    slot: Any,
    target_capability: Any,
) -> dict[str, Any]:
    """Create or update a (node, slot) mapping after full validation.

    Validation ladder (design.md §3.1.e): slot normalization (422 invalid) →
    target strip/shape (422 invalid) → target == forward (400 redirect_self)
    → node existence (404 forward_node_unknown) → target in capability
    catalog (422 target_unknown_capability) → insert-or-update keeping
    ``created_at`` + ``mapping_id``. Returns the post-write row (list keys).
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
                        " created_at, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        (mapping_id, node_id, slot, target, now, now),
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
                    "UPDATE forward_mappings SET target_capability = ?, updated_at = ?"
                    " WHERE mapping_id = ?",
                    (target, now, existing["mapping_id"]),
                )
            )
        row = conn.execute(
            q(
                "SELECT node_id, slot, target_capability, created_at, updated_at"
                " FROM forward_mappings WHERE node_id = ? AND slot = ?",
                (node_id, slot),
            )
        ).fetchone()
        conn.commit()
        return dict(row)
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