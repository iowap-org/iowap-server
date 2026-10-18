"""Scheduler router for task submission, claim, complete, and artifact upload."""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from relay_server.api.v2.security import get_alive_context, get_approved_context
from relay_server.core.artifacts import store_artifact
from relay_server.core.db import get_conn, q
from relay_server.core.events import event_bus
from relay_server.core.forward import (
    ForwardRoutingError,
    is_forward_stage,
    normalize_slot,
    resolve_forward_stages,
)
from relay_server.core.scheduler import Scheduler
from relay_server.models import (
    ArtifactUploadResponse,
    AuthContext,
    ClaimRequest,
    ClaimResponse,
    CompleteRequest,
    NoteRequest,
    NoteResponse,
    PRIORITY_TOP,
    SimpleTaskRequest,
    SimpleTaskResponse,
    StageSummary,
    TaskRequest,
    TaskSummary,
    TaskView,
)

router = APIRouter()


def _apply_forward_redirects(submitter_node_id: str, stages: list[dict[str, Any]]) -> None:
    """T-Forward: resolve 'forward' stages against forward_mappings, in place.

    Fast-path no-op when no stage targets the 'forward' pseudo-capability.
    On refusal publishes one 'forward_refused' event (slot-level refusals
    only) and raises HTTPException with a '<code>: <message>' detail.
    Contracts: design.md §3.3.
    """
    if not any(is_forward_stage(stage) for stage in stages):
        return
    try:
        resolve_forward_stages(submitter_node_id, stages)
    except ForwardRoutingError as exc:
        if exc.code in ("forward_mapping_missing", "forward_mapping_invalid"):
            # Best-effort slot: the forward_slot payload value of the FIRST
            # forward stage; falls back to the raw value when normalization
            # fails, and to None when there is no slot at all.
            slot: Any = None
            for stage in stages:
                if not is_forward_stage(stage):
                    continue
                payload = stage.get("payload") or {}
                raw = payload.get("forward_slot") if isinstance(payload, dict) else None
                try:
                    slot = normalize_slot(raw)
                except ForwardRoutingError:
                    slot = raw
                break
            event_bus.publish_sync(
                "forward_refused",
                {"node_id": submitter_node_id, "slot": slot, "reason": exc.code},
            )
        raise HTTPException(
            status_code=exc.http_status, detail=f"{exc.code}: {exc.message}"
        ) from exc


@router.post("/tasks", response_model=TaskView)
async def scheduler_create_task(
    body: TaskRequest,
    ctx: AuthContext = Depends(get_approved_context),
):
    """Submit a new multi-stage task."""
    stages = [s.model_dump() for s in body.stages]
    # T-Forward: resolve 'forward' stages against the submitter's mappings.
    _apply_forward_redirects(ctx.node_id, stages)
    result = Scheduler.create_task(
        task_name=body.task_name,
        stages=stages,
        priority=body.priority,
        owner_node_id=body.owner_node_id,
        timeout_seconds=body.timeout_seconds,
        submitted_by=ctx.node_id,
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
    # T-005g + T-227: non-admin nodes see their own tasks, the tasks they
    # submitted (sender view) and ownerless ones (fan-out contract).
    scope = None if ctx.is_admin else ctx.node_id
    return {"tasks": Scheduler.list_tasks(status=status, owner_node_id=scope, submitted_by=scope), "viewer": ctx.node_id}


@router.get("/tasks/{task_id}", response_model=TaskView)
async def scheduler_get_task(
    task_id: str,
    ctx: AuthContext = Depends(get_approved_context),
):
    scope = None if ctx.is_admin else ctx.node_id
    task = Scheduler.get_task(task_id, owner_node_id=scope, submitted_by=scope)
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return _task_to_view(task)


@router.post("/tasks/{task_id}/notes")
async def scheduler_add_note(
    task_id: str,
    body: NoteRequest,
    ctx: AuthContext = Depends(get_alive_context),
):
    """Append a free-form note to a task (T-052 mini-chat between nodes).

    Any approved node can add a note; the note is visible to every node
    that subsequently queries the task via ``GET /tasks/{task_id}``.
    Returns ``404`` when the task does not exist.
    """
    result = Scheduler.add_note(task_id=task_id, node_id=ctx.node_id, message=body.message, kind=body.kind)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return result


@router.post("/claim", response_model=ClaimResponse)
async def scheduler_claim(
    body: ClaimRequest = ClaimRequest(),
    ctx: AuthContext = Depends(get_alive_context),
):
    """Claim the next available stage matching the node's capabilities."""
    stage = Scheduler.claim_stage(ctx.node_id, capability=body.capability, capability_type=body.capability_type)
    if stage:
        return ClaimResponse(claimed=True, stage=StageSummary(**stage))
    return ClaimResponse(claimed=False, stage=None)


@router.post("/stages/{stage_id}/complete", response_model=StageSummary)
async def scheduler_complete_stage(
    stage_id: str,
    body: CompleteRequest,
    ctx: AuthContext = Depends(get_alive_context),
):
    stage = Scheduler.complete_stage(
        stage_id=stage_id,
        node_id=ctx.node_id,
        result=body.result,
    )
    if not stage:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Stage not found, not claimed by this node, or not in claimable/completable status",
        )
    return StageSummary(**stage)


@router.post("/artifacts/{task_id}")
async def scheduler_upload_artifact(
    task_id: str,
    file: UploadFile = File(...),
    stage_id: Optional[str] = None,
    ctx: AuthContext = Depends(get_alive_context),
):
    """Upload an artifact attached to a task (and optionally a stage).

    T-005g + T-227: non-admin nodes may only attach artifacts to their
    own, sender or ownerless tasks — a foreign-owned task is
    indistinguishable from a missing one (404).
    """
    scope = None if ctx.is_admin else ctx.node_id
    if scope is not None:
        task_owner = Scheduler.artifact_task_owner(task_id)
        task_sender = Scheduler.artifact_task_sender(task_id)
        if (
            task_owner is not None
            and task_owner != scope
            and task_sender != scope
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
        if task_owner is None:
            # artifact_task_owner returns None for missing AND ownerless
            # tasks — disambiguate before storing (FK requires the task row).
            conn = get_conn()
            try:
                exists = conn.execute(
                    q("SELECT 1 FROM tasks WHERE task_id = ?", (task_id,))
                ).fetchone()
            finally:
                conn.close()
            if not exists:
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
    ctx: AuthContext = Depends(get_alive_context),
):
    # T-005g + T-227: non-admin nodes only see artifacts of their own,
    # sender or ownerless tasks.
    scope = None if ctx.is_admin else ctx.node_id
    return {"artifacts": Scheduler.list_artifacts_scoped(task_id, owner_node_id=scope, submitted_by=scope), "viewer": ctx.node_id}


@router.delete("/artifacts/{artifact_id}")
async def scheduler_delete_artifact(
    artifact_id: str,
    ctx: AuthContext = Depends(get_alive_context),
):
    # T-005g + T-227: non-admin nodes may only delete artifacts of their
    # own, sender or ownerless tasks — foreign artifacts report as not found.
    scope = None if ctx.is_admin else ctx.node_id
    ok = Scheduler.delete_artifact_scoped(artifact_id, owner_node_id=scope, submitted_by=scope)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    return {"status": "deleted", "artifact_id": artifact_id}


def _task_to_view(task: Dict[str, Any]) -> TaskView:
    return TaskView(
        task=TaskSummary(**task),
        stages=[StageSummary(**s) for s in task["stages"]],
        artifacts=task["artifacts"],
        notes=[NoteResponse(**n) for n in task.get("notes", [])],
    )


# ── Simple Task (Ein-Stage) ────────────────────────────────────

@router.post("/task-simple", response_model=SimpleTaskResponse)
async def scheduler_create_simple_task(
    body: SimpleTaskRequest,
    ctx: AuthContext = Depends(get_approved_context),
):
    """
    Submit a single-stage task.

    Creates a task whose single stage awaits a node with a matching
    capability. T-229: the task is stored pending even while the
    capability is busy/unavailable (queued instead of hard 503) and
    defaults to the top priority (10) — claimed first, equal-priority
    ties resolve FIFO by creation order (see claim ordering in core).
    With an explicitly pinned ``priority`` (0–10) that value wins.
    """
    from relay_server.core.discovery import get_capability_by_name

    # T-229 (T-228 decision, Ronny 2026-10-15): a busy/unavailable
    # capability no longer hard-503s the submit — the task is stored
    # pending and claimed as soon as the capability frees up.
    # task-simple means "do it NOW", so it queues at the TOP priority
    # (10) unless the consumer pins one explicitly; equal-priority ties
    # resolve FIFO by creation order (claim ordering in core).
    priority = body.priority if body.priority is not None else PRIORITY_TOP

    task_name = body.name or f"simple-{body.capability}"
    stages = [
        {
            "stage_name": "main",
            "capability": body.capability,
            "depends_on": None,
            "timeout_seconds": body.timeout_seconds,
            "payload": body.payload,
        }
    ]

    # T-Forward: resolve the 'forward' pseudo-capability against the
    # submitter's mappings (mutates stages[0] in place on success).
    _apply_forward_redirects(ctx.node_id, stages)

    capability = stages[0]["capability"]
    cap = get_capability_by_name(capability)
    if not cap:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Capability '{capability}' not found",
        )

    result = Scheduler.create_task(
        task_name=task_name,
        stages=stages,
        priority=priority,
        owner_node_id=body.owner_node_id,
        timeout_seconds=body.timeout_seconds,
        idempotency_key=body.idempotency_key,
        submitted_by=ctx.node_id,
    )

    task_detail = Scheduler.get_task(result["task_id"])
    if not task_detail or not task_detail["stages"]:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Task was created, but stage was not found",
        )

    return SimpleTaskResponse(
        task_id=result["task_id"],
        stage_id=task_detail["stages"][0]["stage_id"],
        status="pending",
        capability=capability,
    )