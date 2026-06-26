"""
Task- & Artifact-Modelle – erweiterte Versionen für Worker-Node-Workflows.

task-simple:  Ein-Stage-Task (capability + payload)
task-complex: Multi-Stage-Task (TaskRequest existiert bereits)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

from relay_server.models.capability import CapabilityInputSchema


# ── Simple Task ────────────────────────────────────────────────

class SimpleTaskRequest(BaseModel):
    """Ein einstufiger Task – Capability + Payload direkt."""

    capability: str = Field(..., min_length=1)
    payload: Dict[str, Any] = Field(default_factory=dict)
    name: str = ""
    priority: int = Field(default=0, ge=0, le=10)
    timeout_seconds: Optional[int] = None
    owner_node_id: Optional[str] = Field(None, min_length=1)
    idempotency_key: Optional[str] = Field(
        None,
        description="Eindeutiger Schlüssel – verhindert Duplikate bei Retries",
    )


class SimpleTaskResponse(BaseModel):
    """Antwort auf task-simple."""

    task_id: str
    stage_id: str
    status: str  # pending
    capability: str


# ── Complex Task (bestehend, erweitert) ────────────────────────

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


# ── Shared validation helpers ──────────────────────────────────

def validate_simple_task(task: SimpleTaskRequest, schema: dict) -> list[str]:
    """
    Validiert ein SimpleTaskRequest gegen ein CapabilityInputSchema.
    Gibt eine Liste von Fehlermeldungen zurück (leer = OK).
    """
    input_schema = CapabilityInputSchema(fields=schema.get("fields", {}))
    return input_schema.validate_payload(task.payload)