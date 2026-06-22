"""Pydantic models for the AI-Relay-Service v2 API."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator
from typing import Any


class Capability(BaseModel):
    name: str
    version: str = "1.0.0"
    consumes: Optional[List[str]] = None
    produces: Optional[List[str]] = None


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
    node_id: str = Field(..., min_length=1, max_length=128)
    registration_secret: str = Field(..., min_length=1)


class RegistrationStatusResponse(BaseModel):
    node_id: str
    node_name: str
    status: str
    token: Optional[str] = None
    token_type: Optional[str] = None
    expires_at: Optional[str] = None
    registration_secret: Optional[str] = None
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


# --- Scheduler models ---


class StageInput(BaseModel):
    stage_name: str
    capability: str
    depends_on: Optional[List[str]] = None
    timeout_seconds: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None


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


class CapabilityStatus(BaseModel):
    name: str
    version: str = "1.0.0"
    available: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def _accept_string(cls, value: Any):
        if isinstance(value, str):
            return {"name": value}
        return value


class HeartbeatRequest(BaseModel):
    load: Optional[float] = Field(None, ge=0.0, le=1.0)
    queue_depth: Optional[int] = Field(None, ge=0)
    available: Optional[bool] = None
    endpoint: Optional[str] = Field(None, max_length=2048)
    capabilities: Optional[List[CapabilityStatus]] = None


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
