"""
Task & artifact models – extended versions for worker-node workflows.

task-simple:  single-stage task (capability + payload)
task-complex: multi-stage task (TaskRequest already exists)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from relay_server.config import settings
from relay_server.models.capability import CapabilityInputSchema

# ── Simple Task ────────────────────────────────────────────────

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


# ── Complex Task (existing, extended) ────────────────────────

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
    retry_count: int = Field(
        0,
        description="How many times this stage has been released back to pending "
                    "after a failed/expired claim (T-060). Once it exceeds "
                    "max_retries the stage is failed permanently.",
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
    created_by: Optional[str] = None


class NoteResponse(BaseModel):
    """A single task note (T-052 mini-chat between collaborating nodes)."""

    id: int
    node_id: str
    message: str
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


class ArtifactUploadResponse(BaseModel):
    artifact_id: str
    name: str
    path: str
    size_bytes: int
    mime_type: Optional[str] = None
    created_by: str


# ── Shared validation helpers ──────────────────────────────────

def validate_simple_task(task: SimpleTaskRequest, schema: dict) -> list[str]:
    """
    Validate a SimpleTaskRequest against a CapabilityInputSchema.
    Returns a list of error messages (empty = OK).
    """
    input_schema = CapabilityInputSchema.from_dict(schema)
    _, errors = input_schema.validate_payload(task.payload)
    return errors
