"""Scheduler router for task submission, claim, complete, and artifact upload."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from relay_server.api.v2.security import get_approved_context
from relay_server.core.artifacts import delete_artifact, list_artifacts, store_artifact
from relay_server.core.db import get_conn
from relay_server.core.scheduler import Scheduler
from relay_server.models import (
    ArtifactUploadResponse,
    AuthContext,
    ClaimRequest,
    ClaimResponse,
    CompleteRequest,
    StageSummary,
    TaskRequest,
    TaskSummary,
    TaskView,
)

router = APIRouter()


@router.post("/tasks", response_model=TaskView)
async def scheduler_create_task(
    body: TaskRequest,
    ctx: AuthContext = Depends(get_approved_context),
):
    """Submit a new multi-stage task."""
    stages = [s.model_dump() for s in body.stages]
    result = Scheduler.create_task(
        task_name=body.task_name,
        stages=stages,
        priority=body.priority,
        owner_node_id=body.owner_node_id or ctx.node_id,
        timeout_seconds=body.timeout_seconds,
    )
    task = Scheduler.get_task(result["task_id"])
    if not task:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Task not found after creation",
        )
    return _task_to_view(task)


@router.get("/tasks")
async def scheduler_list_tasks(
    status: Optional[str] = Query(None),
    ctx: AuthContext = Depends(get_approved_context),
):
    return {"tasks": Scheduler.list_tasks(status=status), "viewer": ctx.node_id}


@router.get("/tasks/{task_id}", response_model=TaskView)
async def scheduler_get_task(
    task_id: str,
    ctx: AuthContext = Depends(get_approved_context),
):
    task = Scheduler.get_task(task_id)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return _task_to_view(task)


@router.post("/claim", response_model=ClaimResponse)
async def scheduler_claim(
    body: ClaimRequest = ClaimRequest(),
    ctx: AuthContext = Depends(get_approved_context),
):
    """Claim the next available stage matching the node's capabilities."""
    stage = Scheduler.claim_stage(ctx.node_id, capability=body.capability)
    if stage:
        return ClaimResponse(claimed=True, stage=StageSummary(**stage))
    return ClaimResponse(claimed=False, stage=None)


@router.post("/stages/{stage_id}/complete", response_model=StageSummary)
async def scheduler_complete_stage(
    stage_id: str,
    body: CompleteRequest,
    ctx: AuthContext = Depends(get_approved_context),
):
    stage = Scheduler.complete_stage(
        stage_id=stage_id,
        node_id=ctx.node_id,
        result=body.result,
    )
    if not stage:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stage not found, not claimed by this node, or not in claimed status",
        )
    return StageSummary(**stage)


@router.post("/artifacts/{task_id}")
async def scheduler_upload_artifact(
    task_id: str,
    file: UploadFile = File(...),
    stage_id: Optional[str] = None,
    ctx: AuthContext = Depends(get_approved_context),
):
    """Upload an artifact attached to a task (and optionally a stage)."""
    # Verify task exists.
    conn = get_conn()
    try:
        row = conn.execute("SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    content = await file.read()
    result = store_artifact(
        name=file.filename or "unnamed",
        content=content,
        mime_type=file.content_type,
        task_id=task_id,
        stage_id=stage_id,
        created_by=ctx.node_id,
    )
    return ArtifactUploadResponse(**result)


@router.get("/artifacts/{task_id}")
async def scheduler_list_artifacts(
    task_id: str,
    ctx: AuthContext = Depends(get_approved_context),
):
    return {"artifacts": list_artifacts(task_id=task_id), "viewer": ctx.node_id}


@router.delete("/artifacts/{artifact_id}")
async def scheduler_delete_artifact(
    artifact_id: str,
    ctx: AuthContext = Depends(get_approved_context),
):
    ok = delete_artifact(artifact_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return {"status": "deleted", "artifact_id": artifact_id}


def _task_to_view(task: Dict[str, Any]) -> TaskView:
    return TaskView(
        task=TaskSummary(**task),
        stages=[StageSummary(**s) for s in task["stages"]],
        artifacts=task["artifacts"],
    )
