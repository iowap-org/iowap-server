"""Central status registry for all entity types.

Each status has a name and a category. The scheduler and dashboard
query by category instead of hardcoded string lists, making it easy
to add new status values without touching business logic.

Phase 18 (T-078) introduces this registry as the single source of
truth for which statuses exist for nodes, tasks, stages and users,
which category they belong to (AVAILABLE / BUSY / PENDING / TERMINAL /
OFFLINE) and which transitions between them are allowed.

The category is what business logic should care about:

* AVAILABLE — the entity is online and ready to accept work
* BUSY      — the entity is online but currently cannot accept work
* PENDING   — waiting for a decision / input / approval
* TERMINAL  — final state, no further transitions
* OFFLINE   — not reachable

Status names are shared across entity types where they overlap
(e.g. ``pending`` exists for nodes, tasks and stages), so the lookup
helpers (:func:`get_category`, :func:`is_terminal`, …) work with any
entity's status string. Callers that need entity-specific reasoning
(e.g. "can this node claim a stage?") use the dedicated helpers
:func:`node_can_claim` / :func:`node_is_claimable`.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, List


class StatusCategory(enum.Enum):
    AVAILABLE = "available"   # online, idle, approved, active
    BUSY = "busy"             # busy, running, claimed, maintenance
    PENDING = "pending"       # pending, awaiting_subtasks, needs_input, accepted
    TERMINAL = "terminal"     # completed, failed, timed_out, cancelled
    OFFLINE = "offline"       # offline, inactive


@dataclass
class StatusDef:
    name: str
    category: StatusCategory
    allowed_transitions: List[str] = field(default_factory=list)


# ── Node statuses ────────────────────────────────────────────────

NODE_STATUSES: Dict[str, StatusDef] = {
    "offline":     StatusDef("offline",     StatusCategory.OFFLINE,   ["pending"]),
    "pending":     StatusDef("pending",     StatusCategory.PENDING,   ["approved", "offline"]),
    "approved":    StatusDef("approved",    StatusCategory.AVAILABLE, ["online", "offline"]),
    "online":      StatusDef("online",      StatusCategory.AVAILABLE, ["busy", "idle", "offline", "maintenance"]),
    "idle":        StatusDef("idle",        StatusCategory.AVAILABLE, ["busy", "online", "offline"]),
    "busy":        StatusDef("busy",         StatusCategory.BUSY,     ["idle", "online", "offline"]),
    "maintenance": StatusDef("maintenance",  StatusCategory.BUSY,     ["offline"]),
}

# ── Task statuses ────────────────────────────────────────────────

TASK_STATUSES: Dict[str, StatusDef] = {
    "pending":           StatusDef("pending",           StatusCategory.PENDING,   ["accepted", "running", "cancelled"]),
    "accepted":          StatusDef("accepted",          StatusCategory.PENDING,   ["running", "awaiting_subtasks", "cancelled"]),
    "running":           StatusDef("running",            StatusCategory.BUSY,     ["completed", "failed", "timed_out", "cancelled"]),
    "awaiting_subtasks": StatusDef("awaiting_subtasks",  StatusCategory.PENDING,   ["running", "cancelled"]),
    "needs_input":       StatusDef("needs_input",        StatusCategory.PENDING,   ["running", "cancelled"]),
    "completed":         StatusDef("completed",          StatusCategory.TERMINAL,  []),
    "failed":            StatusDef("failed",             StatusCategory.TERMINAL,  []),
    "timed_out":         StatusDef("timed_out",          StatusCategory.TERMINAL,  []),
    "cancelled":         StatusDef("cancelled",          StatusCategory.TERMINAL,  []),
}

# ── Stage statuses ──────────────────────────────────────────────

STAGE_STATUSES: Dict[str, StatusDef] = {
    "pending":   StatusDef("pending",   StatusCategory.PENDING,   ["claimed", "accepted", "cancelled"]),
    "claimed":   StatusDef("claimed",    StatusCategory.BUSY,     ["completed", "failed", "timed_out", "pending"]),
    "accepted":  StatusDef("accepted",  StatusCategory.PENDING,   ["completed", "failed", "timed_out"]),
    "completed": StatusDef("completed", StatusCategory.TERMINAL,  []),
    "failed":    StatusDef("failed",    StatusCategory.TERMINAL,  []),
    "timed_out": StatusDef("timed_out", StatusCategory.TERMINAL,  []),
    "cancelled": StatusDef("cancelled", StatusCategory.TERMINAL,  []),
}

# ── User statuses ───────────────────────────────────────────────

USER_STATUSES: Dict[str, StatusDef] = {
    "active":   StatusDef("active",   StatusCategory.AVAILABLE, ["inactive"]),
    "inactive": StatusDef("inactive", StatusCategory.OFFLINE,   ["active"]),
}

# ── Combined lookup ──────────────────────────────────────────────
#
# Status names are NOT unique across entity types: ``pending`` exists
# for nodes, tasks and stages with different allowed transitions. The
# combined ``_ALL`` map therefore keeps the LAST definition seen
# (stage's ``pending``), which is fine for the category helpers
# (``is_terminal`` / ``is_busy`` / …) because the category is the same
# across entities. Transition checks must be entity-specific, so use
# :func:`node_can_transition` / :func:`task_can_transition` /
# :func:`stage_can_transition` (or :func:`can_transition` with an
# explicit ``entity_type``) instead of the generic lookup.

_ALL: Dict[str, StatusDef] = {}
for d in (NODE_STATUSES, TASK_STATUSES, STAGE_STATUSES, USER_STATUSES):
    _ALL.update(d)

# Per-entity registries for transition checks where the shared name
# space would be ambiguous (e.g. node ``pending`` → ``approved`` vs.
# task ``pending`` → ``running``).
_ENTITY_REGISTRIES = {
    "node": NODE_STATUSES,
    "task": TASK_STATUSES,
    "stage": STAGE_STATUSES,
    "user": USER_STATUSES,
}


def get_status(name: str) -> StatusDef | None:
    return _ALL.get(name)


def get_category(name: str) -> StatusCategory | None:
    sd = _ALL.get(name)
    return sd.category if sd else None


def is_terminal(name: str) -> bool:
    sd = _ALL.get(name)
    return sd is not None and sd.category == StatusCategory.TERMINAL


def is_busy(name: str) -> bool:
    sd = _ALL.get(name)
    return sd is not None and sd.category == StatusCategory.BUSY


def is_available(name: str) -> bool:
    sd = _ALL.get(name)
    return sd is not None and sd.category == StatusCategory.AVAILABLE


def is_pending(name: str) -> bool:
    sd = _ALL.get(name)
    return sd is not None and sd.category == StatusCategory.PENDING


def is_offline(name: str) -> bool:
    sd = _ALL.get(name)
    return sd is not None and sd.category == StatusCategory.OFFLINE


def can_transition(from_status: str, to_status: str, entity_type: str | None = None) -> bool:
    """Return whether ``from_status`` → ``to_status`` is an allowed transition.

    Because status names overlap across entity types (``pending`` exists
    for nodes, tasks and stages with different transitions), pass
    ``entity_type`` (``"node"`` / ``"task"`` / ``"stage"`` / ``"user"``)
    for an unambiguous answer. Without it the lookup falls back to the
    combined registry, which may return a different entity's transition
    table — prefer the entity-specific helpers below for new code.
    """
    if entity_type is not None:
        registry = _ENTITY_REGISTRIES.get(entity_type, {})
        sd = registry.get(from_status)
    else:
        sd = _ALL.get(from_status)
    if sd is None:
        return False
    return to_status in sd.allowed_transitions


def node_can_transition(from_status: str, to_status: str) -> bool:
    return can_transition(from_status, to_status, entity_type="node")


def task_can_transition(from_status: str, to_status: str) -> bool:
    return can_transition(from_status, to_status, entity_type="task")


def stage_can_transition(from_status: str, to_status: str) -> bool:
    return can_transition(from_status, to_status, entity_type="stage")


def user_can_transition(from_status: str, to_status: str) -> bool:
    return can_transition(from_status, to_status, entity_type="user")


def node_can_claim(node_status: str) -> bool:
    """A node can claim stages only when its status is AVAILABLE."""
    cat = get_category(node_status)
    return cat == StatusCategory.AVAILABLE


def node_is_claimable(node_status: str) -> bool:
    """A node is a valid claim target (not busy, not offline)."""
    cat = get_category(node_status)
    return cat in (StatusCategory.AVAILABLE, StatusCategory.PENDING)


def statuses_in_category(category: StatusCategory) -> list[str]:
    """Return all registered status names that belong to ``category``.

    Used by SQL-based queries (e.g. the orphaned-stage watchdog) that
    need a concrete ``IN (...)`` list instead of a Python predicate.
    """
    return [name for name, sd in _ALL.items() if sd.category == category]


def node_statuses_in_category(category: StatusCategory) -> list[str]:
    """Return NODE status names that belong to ``category``.

    Prefer this over :func:`statuses_in_category` for SQL queries that
    filter ``nodes.status`` so non-node statuses (e.g. the user status
    ``active``) don't leak into the predicate.
    """
    return [name for name, sd in NODE_STATUSES.items() if sd.category == category]


def node_claim_statuses() -> list[str]:
    """Statuses that allow a node to claim stages (AVAILABLE category)."""
    return node_statuses_in_category(StatusCategory.AVAILABLE)


# ── Dashboard colour mapping ───────────────────────────────────

STATUS_COLORS: Dict[StatusCategory, str] = {
    StatusCategory.AVAILABLE: "ok",     # green
    StatusCategory.BUSY:     "warn",    # yellow
    StatusCategory.PENDING:  "info",    # blue
    StatusCategory.TERMINAL: "muted",   # grey (individual statuses override: completed=ok, failed=bad)
    StatusCategory.OFFLINE:  "bad",     # red
}


def status_color(name: str) -> str:
    """Return the dashboard colour class for a status string.

    Terminal statuses get a per-name override so ``completed`` is
    green and ``failed``/``timed_out``/``cancelled`` are red instead
    of the generic grey used for the TERMINAL category.
    """
    if name in ("completed",):
        return "ok"
    if name in ("failed", "timed_out", "cancelled"):
        return "bad"
    cat = get_category(name)
    if cat is None:
        return "muted"
    return STATUS_COLORS[cat]