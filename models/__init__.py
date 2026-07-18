"""Pydantic models for the AI-Relay-Service v2 API."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from relay_server.config import settings

# ── Capability (server-side, for API requests/responses) ──────

class Capability(BaseModel):
    """Represents a capability of a node in API responses."""

    name: str
    type: Optional[str] = None  # ai | tool | script | workflow | resource
    description: str = ""
    version: str = "1.0.0"
    available: bool | None = None
    input_schema: Optional[dict[str, Any]] = Field(
        None,
        description="Input schema for task validation (from capabilities.yaml)",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class TaskSummary(BaseModel):
    task_id: str
    task_name: str
    status: str
    priority: int
    owner_node_id: Optional[str] = None
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
    result: Optional[Dict[str, Any]] = None


class ArtifactReference(BaseModel):
    artifact_id: str
    name: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    created_by: Optional[str] = None


class TaskView(BaseModel):
    task: TaskSummary
    stages: List[StageSummary]
    artifacts: List[ArtifactReference]


class ClaimRequest(BaseModel):
    capability: Optional[str] = None
    capability_type: Optional[str] = None  # Filter: only capabilities of this type


class ClaimResponse(BaseModel):
    claimed: bool
    stage: Optional[StageSummary] = None


class CompleteRequest(BaseModel):
    result: Optional[Dict[str, Any]] = None
    artifacts: Optional[List[str]] = None


class ArtifactUploadResponse(BaseModel):
    artifact_id: str
    name: str
    path: str
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
    description: str = ""
    version: str = "1.0.0"
    available: bool = True
    input_schema: Optional[dict[str, Any]] = None
    dashboard_page: bool = False
    nodes: list[DiscoveryNode] = Field(default_factory=list)


class DiscoveryResponse(BaseModel):
    """Response for GET /discovery/capabilities."""

    capabilities: list[DiscoveryCapability]


class DiscoveryDetailResponse(BaseModel):
    """Response for GET /discovery/capabilities/{name}."""

    name: str
    type: str
    description: str
    version: str
    available: bool
    input_schema: Optional[dict[str, Any]] = None
    dashboard_page: bool = False
    nodes: list[DiscoveryNode]


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
    priority: int = Field(default=0, ge=0, le=10)
    timeout_seconds: Optional[int] = None
    owner_node_id: Optional[str] = Field(None, min_length=1)
    idempotency_key: Optional[str] = Field(
        None,
        description="Unique key – prevents duplicates on retries",
    )

    @model_validator(mode='after')
    def validate_payload_size(self):
        payload_str = json.dumps(self.payload)
        if len(payload_str) > settings.max_payload_bytes:
            raise ValueError(
                f"Payload exceeds maximum size of {settings.max_payload_bytes} bytes"
            )
        return self


class SimpleTaskResponse(BaseModel):
    """Response for task-simple."""

    task_id: str
    stage_id: str
    status: str  # pending
    capability: str


# ── Heartbeat / Status ──────────────────────────────────────────

class CapabilityStatus(BaseModel):
    name: str
    version: str = "1.0.0"
    available: Optional[bool] = None
    dashboard_page: bool = False

    @model_validator(mode="before")
    @classmethod
    def _accept_string(cls, value: Any):
        if isinstance(value, str):
            return {"name": value}
        return value


class HeartbeatRequest(BaseModel):
    load: Optional[float] = Field(None, ge=0.0, le=100.0)
    queue_depth: Optional[int] = Field(None, ge=0)
    available: Optional[bool] = None
    endpoint: Optional[str] = Field(None, max_length=2048)
    capabilities: Optional[List[CapabilityStatus]] = None


class NodeHeartbeatRequest(BaseModel):
    """Extended heartbeat model for worker nodes with full capability data."""
    load: Optional[float] = Field(None, ge=0.0, le=100.0)
    queue_depth: Optional[int] = Field(None, ge=0)
    available: Optional[bool] = None
    endpoint: Optional[str] = Field(None, max_length=2048)
    capabilities: Optional[List[dict[str, Any]]] = None


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
