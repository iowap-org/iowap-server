"""Pydantic models for the IOWAP v2 API."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from relay_server.config import settings

# T-229: top of the claim order — claim ordering is priority DESC,
# created_at ASC, sequence ASC (FIFO among equal priority).
PRIORITY_TOP = 10

# T-274 P8: board_task_id transport limits (metadata validation is loud 422).
BOARD_TASK_ID_MAX_CHARS = 255


def _validate_board_task_id_metadata(metadata: Optional[Dict[str, Any]]) -> None:
    """T-274 P8: loud 422 for a set-but-invalid ``metadata.board_task_id``.

    Valid = a non-empty string of at most 255 characters (a board row label
    the submitter wants linked). Wrong types (numbers, dicts), empty strings
    and overlong labels are rejected at the transport layer — they never
    reach the tasks row silently mangled. An absent metadata / key stays
    valid: board linking is opt-in.
    """
    if metadata is None or "board_task_id" not in metadata:
        return
    value = metadata["board_task_id"]
    if (
        not isinstance(value, str)
        or not (1 <= len(value) <= BOARD_TASK_ID_MAX_CHARS)
    ):
        raise ValueError(
            "metadata.board_task_id must be a non-empty string of at most "
            f"{BOARD_TASK_ID_MAX_CHARS} characters"
        )

# ── Capability (server-side, for API requests/responses) ──────

class Capability(BaseModel):
    """Represents a capability of a node in API responses."""

    name: str
    type: Optional[str] = None  # ai | tool | script | workflow | resource
    description: Optional[str] = None
    version: str = "1.0.0"
    available: bool | None = None
    input_schema: Optional[dict[str, Any]] = Field(
        None,
        description="Input schema for task validation (from capabilities.yaml)",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    # T-004 (iowap-flow): result_path_hints — Dot-Paths into the task
    # result (e.g. ["result.answer"]). Pure metadata for consumers like
    # the flow planner; the server mediates and routes only.
    result_path_hints: Optional[List[str]] = Field(
        None,
        description="Dot-paths into the task result (hints for template interpolation)",
    )


# ── Node Registration ───────────────────────────────────────────

class NodeRegistration(BaseModel):
    node_name: str = Field(..., min_length=1, max_length=256)
    endpoint: Optional[str] = None
    capabilities: List[Capability] = Field(default_factory=list)
    role: str = "worker"


class AdminNodeRegistration(BaseModel):
    node_name: str = Field(..., min_length=1, max_length=256)
    endpoint: Optional[str] = None
    capabilities: List[Capability] = Field(default_factory=list)
    bootstrap_secret: str = Field(..., min_length=1)


# ── Auth & Tokens ───────────────────────────────────────────────

class TokenResponse(BaseModel):
    node_id: str
    node_name: str
    status: str
    token_type: str
    token: str
    expires_at: str


class AdminNodeRegistrationResponse(TokenResponse):
    pass


class NodeRegistrationResponse(TokenResponse):
    registration_secret: Optional[str] = None


class NodeApproval(BaseModel):
    role: Optional[str] = None
    capabilities: Optional[List[Capability]] = None
    endpoint: Optional[str] = None


class RegistrationStatusRequest(BaseModel):
    node_id: Optional[str] = Field(None, min_length=1, max_length=128)
    registration_secret: Optional[str] = Field(None, min_length=1)


class RegistrationStatusResponse(BaseModel):
    node_id: str
    node_name: str
    status: str
    rt_valid_until: Optional[str] = None
    rs_valid_until: Optional[str] = None
    message: str


class RefreshRequest(BaseModel):
    node_id: Optional[str] = Field(None, min_length=1, max_length=128)
    requested_credential: str = Field(..., pattern="^(runtime_token|registration_secret)$")
    registration_secret: Optional[str] = Field(None, min_length=1)


class RefreshResponse(BaseModel):
    node_id: str
    node_name: str
    token_type: str
    token: str
    expires_at: Optional[str] = None
    registration_secret: Optional[str] = None
    registration_secret_expires_at: Optional[str] = None
    message: str


class AuthContext(BaseModel):
    token_id: str
    node_id: str
    node_name: str
    endpoint: Optional[str]
    capabilities: List[Capability]
    status: str
    role: str
    token_type: str
    pending: bool
    expires_at: Optional[str] = None
    user_id: Optional[str] = None
    username: Optional[str] = None
    groups: List[str] = Field(default_factory=list)

    @property
    def is_admin(self) -> bool:
        if self.user_id == "__master__":
            return True
        return self.role == "admin" and self.status in ("approved", "online") and not self.pending

    @property
    def is_approved(self) -> bool:
        if self.user_id == "__master__":
            return True
        return self.status in ("approved", "online") and not self.pending


# ── Scheduler models ────────────────────────────────────────────

class StageInput(BaseModel):
    stage_name: str
    capability: str
    depends_on: Optional[List[str]] = None
    timeout_seconds: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None

    @model_validator(mode='after')
    def validate_payload_size(self):
        if self.payload is not None:
            payload_str = json.dumps(self.payload)
            if len(payload_str) > settings.max_payload_bytes:
                raise ValueError(
                    f"Payload exceeds maximum size of {settings.max_payload_bytes} bytes"
                )
        return self


class TaskRequest(BaseModel):
    task_name: str
    stages: List[StageInput]
    priority: int = Field(default=0, ge=0, le=10)
    owner_node_id: Optional[str] = None
    timeout_seconds: Optional[int] = None
    # T-274 P8: optional task metadata. ``metadata.board_task_id`` carries
    # the project-board row label to link the relay task to (persisted on
    # tasks.board_task_id, readable in TaskView/API); other keys are kept
    # but have no relay-side meaning. An absent metadata stays the old shape.
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_board_task_id(self):
        _validate_board_task_id_metadata(self.metadata)
        return self


class TaskSummary(BaseModel):
    task_id: str
    task_name: str
    status: str
    priority: int
    owner_node_id: Optional[str] = None
    submitted_by: Optional[str] = Field(
        None,
        description="Node that submitted the task (T-227 sender view; server-derived "
                    "from the auth context, not client-suppliable). NULL for legacy tasks.",
    )
    # T-274 P8: project-board row label from the submit-time
    # ``metadata.board_task_id``; NULL = no board link.
    board_task_id: Optional[str] = None
    created_at: str
    updated_at: str


class StageSummary(BaseModel):
    stage_id: str
    task_id: str
    stage_name: str
    capability: str
    status: str
    depends_on: Optional[List[str]] = None
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    completed_at: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    forward_slot: Optional[str] = Field(
        None,
        description="T-Forward: normalized forward slot when this stage was a redirected "
                    "'forward' stage; NULL otherwise.",
    )
    result: Optional[Dict[str, Any]] = None
    retry_count: int = Field(
        0,
        description="How many times this stage has been released back to pending "
                    "after a failed/expired claim (T-060). Once it exceeds "
                    "max_retries the stage is failed permanently.",
    )
    # T-274 P7: schema warn — {"state": "schema_violation", ...violations} or
    # {"state": "unvalidated", ...reason}. NULL means validated: absence of a
    # warn IS the validated state.
    schema_violation: Optional[Dict[str, Any]] = Field(
        None,
        description="T-274 P7 submit-time schema warn on this stage; NULL = validated.",
    )
    capability_details: Optional[Dict[str, Any]] = Field(
        None,
        description="Resolved capability metadata (name, type, description, input_schema) "
                    "advertised by the node that claimed (or can claim) this stage. "
                    "Populated by the scheduler on claim and task-view responses.",
    )


class ArtifactReference(BaseModel):
    artifact_id: str
    name: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    # T-005g: checksum identifies the content; no storage path is exposed.
    checksum: Optional[str] = None
    created_by: Optional[str] = None


class NoteResponse(BaseModel):
    """A single task note (T-052 mini-chat between collaborating nodes)."""

    id: int
    node_id: str
    message: str
    kind: str = "info"
    created_at: str


class TaskView(BaseModel):
    task: TaskSummary
    stages: List[StageSummary]
    artifacts: List[ArtifactReference]
    notes: List[NoteResponse] = Field(default_factory=list)


class ClaimRequest(BaseModel):
    capability: Optional[str] = None
    capability_type: Optional[str] = None  # Filter: only capabilities of this type


class ClaimResponse(BaseModel):
    claimed: bool
    stage: Optional[StageSummary] = None


class CompleteRequest(BaseModel):
    result: Optional[Dict[str, Any]] = None
    artifacts: Optional[List[str]] = None


class NoteRequest(BaseModel):
    """Body for POST /relay/v2/scheduler/tasks/{task_id}/notes (T-052)."""

    message: str = Field(..., min_length=1, max_length=2000)
    kind: str = Field(default="info", description="Note kind: info|progress|longrun (T-154)")


class ArtifactUploadResponse(BaseModel):
    artifact_id: str
    name: str
    # T-005g: no ``path`` field — the server-internal storage path must not
    # leak into API responses; ``checksum`` identifies the stored content.
    checksum: Optional[str] = None
    size_bytes: int
    mime_type: Optional[str] = None
    created_by: str


# ── Discovery models ────────────────────────────────────────────

class DiscoveryNode(BaseModel):
    """A node that offers a specific capability."""

    node_id: str
    node_name: str
    available: bool = True
    load: float = 0.0
    queue_depth: int = 0
    last_seen: str  # ISO format
    config: dict[str, Any] = Field(default_factory=dict)


class DiscoveryCapability(BaseModel):
    """A capability with all nodes that offer it."""

    name: str
    type: Optional[str] = None
    description: Optional[str] = None
    version: str = "1.0.0"
    available: bool = True
    input_schema: Optional[dict[str, Any]] = None
    nodes: list[DiscoveryNode] = Field(default_factory=list)
    # T-004 (iowap-flow): Dot-Paths into the task result (e.g.
    # ["result.answer"]) — pass-through from node_capabilities for the
    # flow planner; pure metadata, routing-relevant only for consumers.
    result_path_hints: Optional[list[str]] = None


class DiscoveryResponse(BaseModel):
    """Response for GET /discovery/capabilities."""

    capabilities: list[DiscoveryCapability]


class DiscoveryDetailResponse(BaseModel):
    """Response for GET /discovery/capabilities/{name}."""

    name: str
    type: Optional[str] = None
    description: Optional[str] = None
    version: str
    available: bool
    input_schema: Optional[dict[str, Any]] = None
    nodes: list[DiscoveryNode]
    # T-004 (iowap-flow): see DiscoveryCapability.result_path_hints.
    result_path_hints: Optional[list[str]] = None


# ── Simple Task ─────────────────────────────────────────────────

from .capability import (  # noqa: E402, I001
    CapabilityInputField as CapabilityInputField,
    CapabilityInputSchema as CapabilityInputSchema,
)


class SimpleTaskRequest(BaseModel):
    """A single-stage task – capability + payload directly."""

    capability: str = Field(..., min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    name: str = ""
    # T-229: None (absent) = TOP priority (PRIORITY_TOP) — task-simple
    # means "do it NOW", so it is claimed ahead of queued lower-priority
    # work; explicit 0–10 wins. Ties FIFO by creation order.
    priority: int | None = Field(
        default=None, ge=0, le=10, description="None = TOP priority (10); explicit value wins"
    )
    timeout_seconds: Optional[int] = None
    owner_node_id: Optional[str] = Field(None, min_length=1)
    idempotency_key: Optional[str] = Field(
        None,
        description="Unique key – prevents duplicates on retries",
    )
    # T-274 P8: optional metadata — ``board_task_id`` links the relay task
    # to a project-board row (persisted, readable in TaskView/API).
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode='after')
    def validate_payload_size(self):
        payload_str = json.dumps(self.payload)
        if len(payload_str) > settings.max_payload_bytes:
            raise ValueError(
                f"Payload exceeds maximum size of {settings.max_payload_bytes} bytes"
            )
        _validate_board_task_id_metadata(self.metadata)
        return self


class SimpleTaskResponse(BaseModel):
    """Response for task-simple."""

    task_id: str
    stage_id: str
    status: str  # pending
    capability: str
    # T-274 P7: the submit-time schema warn of the stage — never raises.
    # NULL = validated; {"state": "schema_violation"|"unvalidated", ...}.
    schema_violation: Optional[Dict[str, Any]] = None

# ── Forward routing (T-Forward) ────────────────────────────────

class CopyRule(BaseModel):
    """One copy/join binding rule (T-247): copy (1 source) or join
    (>1 sources) submitter payload fields into one target field."""

    model_config = ConfigDict(extra="forbid")

    sources: list[str] = Field(..., min_length=1)
    target: str = Field(..., min_length=1, max_length=255)


class ConstRule(BaseModel):
    """One const binding rule (T-252): write a FIXED ``value`` into one
    target field (``think``/``model`` anhacken).

    T-255 (M1): ``value`` transports EVERY JSON value — str/bool/int/float
    AND list/dict (plus the implicit/explicit-null write from T-252) —
    keeping the transport at the same acceptance ``validate_binding_form``
    (core/forward.py, single semantic authority) now has, so the
    T-254-r2 editor's object-typed const textarea is no longer a dead
    branch. The VALUE-vs-SCHEMA type match lives in
    ``check_binding_schema`` (array→list, object→dict)."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(..., min_length=1, max_length=255)
    value: Any


class BindingRules(BaseModel):
    """Payload binding of one forward mapping (T-247, Ronny form):
    ``{"rules": [{sources, target} | {target, value}, …], "separator": "…"}``.

    T-255 (Audit H1): the transport is a union of EXACT rule shapes —
    ``CopyRule``/``ConstRule``, both ``extra=forbid`` — so a mixed rule
    (``sources`` AND ``value``) is a LOUD 422 at the transport layer
    instead of the previous silent ``value``-drop (extra=ignore parsed it
    as a copy rule). Transport shape only (types/lengths, M7: ≤64 rules,
    separator ≤64 chars) — ALL semantic form rules (distinct sources,
    disjoint targets, padding, separator type) live in core/forward.py
    validate_binding_form (single authority, design §3.1); the PUT
    endpoint hands body.binding over for re-validation."""

    rules: list[CopyRule | ConstRule] = Field(..., min_length=1, max_length=64)
    separator: str = Field("\n\n", max_length=64)


class ForwardMappingPut(BaseModel):
    """Body for PUT /relay/v2/forward-mappings/{node_id}/{slot}
    (T-246 adds the pin, T-247 adds binding)."""

    target_capability: str = Field(..., min_length=1, max_length=255)
    target_node_id: Optional[str] = Field(
        None,
        max_length=64,
        description="T-246: pin the mapping to one concrete node;"
                    " omitted/None/'' = no pin = any capable provider.",
    )
    binding: Optional[BindingRules] = Field(
        None,
        description="T-247: payload binding (Ronny form); omitted/null ="
                    " legacy passthrough (full-row replace, pin D-02 analog).",
    )


class ForwardMappingEntry(BaseModel):
    """One (node, slot) → target_capability mapping row (T-246 pin,
    T-247 binding)."""

    node_id: str
    slot: str
    target_capability: str
    target_node_id: Optional[str] = None
    binding: Optional[dict[str, Any]] = None
    created_at: str
    updated_at: str


class ForwardMappingListResponse(BaseModel):
    """Response for GET /relay/v2/forward-mappings."""

    mappings: list[ForwardMappingEntry] = Field(default_factory=list)


# ── Heartbeat / Status ──────────────────────────────────────────

class RouteDeclaration(BaseModel):
    """A single API route declared by a node in its heartbeat (T-075)."""
    path: str = Field(..., min_length=1, max_length=512)
    method: str = Field(..., pattern="^(GET|POST|PUT|DELETE|PATCH)$")
    auth: str = Field("session", pattern="^(session|node_token|none)$")
    upstream: str = Field(..., max_length=2048)
    description: Optional[str] = Field(None, max_length=256)


class CapabilityStatus(BaseModel):
    name: str
    version: str = "1.0.0"
    available: Optional[bool] = None
    # T-053: optional metadata so the heartbeat can populate the
    # normalized ``node_capabilities`` index with description / type /
    # input_schema for the scheduler's capability_details resolution.
    type: Optional[str] = None
    description: Optional[str] = None
    input_schema: Optional[dict[str, Any]] = None
    # T-274 P7: SHA-256 over the canonical input_schema JSON, carried by the
    # same heartbeat. The submit-time warn gate vouches for a schema basis
    # only when the heartbeat shipped hash AND schema together.
    schema_hash: Optional[str] = None
    # T-164: supported file-transfer modes (inline/artifact/bridge). Stored
    # in node_capabilities.upload_modes so file send/file get can pick the
    # right rung without an extra discovery round-trip.
    upload_modes: Optional[list[str]] = None

    @model_validator(mode="before")
    @classmethod
    def _accept_string(cls, value: Any):
        if isinstance(value, str):
            return {"name": value}
        return value


class HeartbeatRequest(BaseModel):
    load: Optional[float] = Field(None, ge=0.0, le=100.0)
    # T-210: which rung of the node's load chain produced `load`
    # (cgroup2/cgroup/loadavg) — persisted for dashboard display.
    load_source: Optional[str] = Field(None, max_length=16)
    queue_depth: Optional[int] = Field(None, ge=0)
    available: Optional[bool] = None
    endpoint: Optional[str] = Field(None, max_length=2048)
    capabilities: Optional[List[CapabilityStatus]] = None
    node_name: Optional[str] = Field(None, max_length=128)
    description: Optional[str] = Field(None, max_length=1024)
    routes: Optional[List[RouteDeclaration]] = None
    # T-081: explicit node status request (e.g. "busy"/"idle" from
    # `node-cli node busy`/`idle`). Validated against the central
    # status registry on the server side; invalid transitions are
    # silently ignored.
    status: Optional[str] = Field(None, max_length=64)
    # T-081: per-node load ceiling used by the auto-busy logic. When
    # ``load`` stays at or above this value for N consecutive
    # heartbeats the node is transitioned to "busy" automatically.
    load_cap: Optional[float] = Field(None, ge=0.0, le=1000000.0)
    # T-Forward-2: node-declared forward slots from node.yaml
    # (``[{slot: "1", label: ..., description: ...}, ...]``). Optional;
    # when present the relay replaces the node's declared rows.
    forward_slots: Optional[List[Dict[str, Any]]] = None
    # T-274 P4/P6: aggregated bridge outcome counters, keyed by bridge
    # (``{"a2a": {"delivered": …, "dropped": …, "failed": …,
    # "pending_count": …, "oldest_pending_age_seconds": …}}``). Counters
    # only (K3) — the raw ledger stays on the node. Persisted on the nodes
    # row and exposed via the node-status API / dashboard.
    bridge_outcomes: Optional[Dict[str, Any]] = Field(
        None,
        description="Wrapper outcome counters per bridge (delivered/dropped/"
                    "failed, pending count + oldest pending age). Counters only, "
                    "no message bodies.",
    )


class NodeHeartbeatRequest(BaseModel):
    """Extended heartbeat model for worker nodes with full capability data."""
    load: Optional[float] = Field(None, ge=0.0, le=100.0)
    # T-210: which rung of the node's load chain produced `load`
    # (cgroup2/cgroup/loadavg) — persisted for dashboard display.
    load_source: Optional[str] = Field(None, max_length=16)
    queue_depth: Optional[int] = Field(None, ge=0)
    available: Optional[bool] = None
    endpoint: Optional[str] = Field(None, max_length=2048)
    capabilities: Optional[List[dict[str, Any]]] = None
    node_name: Optional[str] = Field(None, max_length=128)
    description: Optional[str] = Field(None, max_length=1024)
    routes: Optional[List[dict[str, Any]]] = None
    # T-081: explicit node status request + load ceiling (see HeartbeatRequest).
    status: Optional[str] = Field(None, max_length=64)
    load_cap: Optional[float] = Field(None, ge=0.0, le=1000000.0)
    # T-Forward-2: node-declared forward slots (same shape/semantics as
    # HeartbeatRequest).
    forward_slots: Optional[List[Dict[str, Any]]] = None
    # T-274 P4/P6: aggregated bridge outcome counters (same shape/semantics
    # as HeartbeatRequest.bridge_outcomes) — counters only, no message data.
    bridge_outcomes: Optional[Dict[str, Any]] = Field(
        None,
        description="Wrapper outcome counters per bridge (delivered/dropped/"
                    "failed, pending count + oldest pending age). Counters only, "
                    "no message bodies.",
    )


# ── Presence ────────────────────────────────────────────────────

class PresenceActivity(BaseModel):
    name: Optional[str] = None
    detail: Optional[str] = None
    task_id: Optional[str] = None
    stage_id: Optional[str] = None


class PresenceUpdateRequest(BaseModel):
    status: Optional[str] = Field(None, max_length=64)
    mood: Optional[str] = Field(None, max_length=64)
    activity: Optional[PresenceActivity] = None
    progress: Optional[int] = Field(None, ge=0, le=100)
    eta_seconds: Optional[int] = Field(None, ge=0)
    next_available: Optional[str] = Field(None, max_length=64)


# ── Storage ─────────────────────────────────────────────────────

class StorageStatusResponse(BaseModel):
    node_id: str
    node_name: str
    storage_path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    file_count: int


class StorageFileReference(BaseModel):
    artifact_id: str
    name: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    created_by: Optional[str] = None


# ── Chunked Upload ──────────────────────────────────────────────

class ChunkedInitRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=512)
    mime_type: Optional[str] = Field(None, max_length=256)
    total_chunks: int = Field(..., gt=0, le=10000)


class ChunkedChunkRequest(BaseModel):
    chunk_index: int = Field(..., ge=0)
    # base64-encoded chunk bytes (raw binary would break JSON transport)
    data_b64: str = Field(..., min_length=0)


class ChunkedCompleteRequest(BaseModel):
    checksum: Optional[str] = Field(None, description="Optional SHA256 hex digest of the assembled file")
