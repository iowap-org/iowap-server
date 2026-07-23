"""Scheduler core logic — DAG staging, capability matching, claim/complete."""

import functools
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from relay_server.config import settings
from relay_server.core.db import get_conn, get_capability_details, get_node_capability_names
from relay_server.core.events import event_bus


# ---------------------------------------------------------------------------
# Retry helper for SQLite lock contention
# ---------------------------------------------------------------------------

_LOCKED_RETRIES = 5
_LOCKED_BASE_DELAY = 0.05


def _retry_db_write(func):
    """Retry a DB write on ``database is locked`` with exponential backoff."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_error = None
        delay = _LOCKED_BASE_DELAY
        for attempt in range(_LOCKED_RETRIES):
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc) and "locked" not in str(exc):
                    raise
                last_error = exc
                if attempt < _LOCKED_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
        raise last_error  # type: ignore[misc]

    return wrapper


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_time(dt: datetime) -> str:
    return dt.isoformat()


def _generate_id(prefix: str = "id") -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _serialize(value: Any) -> Optional[str]:
    return json.dumps(value) if value is not None else None


def _parse(value: Optional[str]) -> Optional[Any]:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


class Scheduler:
    """Task scheduler with DAG stages."""

    @staticmethod
    @_retry_db_write
    def create_task(
        task_name: str,
        stages: List[Dict[str, Any]],
        priority: int = 0,
        owner_node_id: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a new task and decompose it into stages."""
        task_id = _generate_id("task")
        now = _format_time(_now())
        default_timeout = timeout_seconds or settings.default_timeout_seconds

        conn = get_conn()
        try:
            conn.execute(
                """
                INSERT INTO tasks (task_id, task_name, status, priority, owner_node_id,
                                   timeout_seconds, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, task_name, "pending", priority, owner_node_id, default_timeout, now, now),
            )

            # Build stage records.
            stage_records = []
            for idx, stage in enumerate(stages):
                stage_id = _generate_id("stage")
                deps = stage.get("depends_on")
                stage_records.append(
                    {
                        "stage_id": stage_id,
                        "stage_name": stage["stage_name"],
                        "capability": stage["capability"],
                        "depends_on": _serialize(deps),
                        "status": "pending",
                        "sequence": idx,
                        "timeout_seconds": stage.get("timeout_seconds", default_timeout),
                        "payload": _serialize(stage.get("payload")),
                    }
                )

            # Linear DAG for MVP: first stage has no deps, each subsequent depends on previous.
            for idx, rec in enumerate(stage_records):
                if rec["depends_on"] is None and idx > 0:
                    rec["depends_on"] = _serialize([stage_records[idx - 1]["stage_id"]])

            for rec in stage_records:
                conn.execute(
                    """
                    INSERT INTO task_stages
                    (stage_id, task_id, stage_name, capability, depends_on, status,
                     sequence, timeout_seconds, payload, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rec["stage_id"],
                        task_id,
                        rec["stage_name"],
                        rec["capability"],
                        rec["depends_on"],
                        rec["status"],
                        rec["sequence"],
                        rec["timeout_seconds"],
                        rec["payload"],
                        now,
                        now,
                    ),
                )

            conn.commit()
            event_bus.publish_sync("task_created", {"task_id": task_id, "task_name": task_name})
            return {"task_id": task_id, "status": "pending", "stage_count": len(stage_records)}
        finally:
            conn.close()

    @staticmethod
    def list_tasks(status: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = get_conn()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY priority DESC, created_at ASC",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY priority DESC, created_at ASC"
                ).fetchall()
            return [_task_row_to_dict(r) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def get_task(task_id: str) -> Optional[Dict[str, Any]]:
        conn = get_conn()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            task = _task_row_to_dict(row)
            stage_rows = conn.execute(
                "SELECT * FROM task_stages WHERE task_id = ? ORDER BY sequence ASC",
                (task_id,),
            ).fetchall()
            task["stages"] = []
            for r in stage_rows:
                stage = _stage_row_to_dict(r)
                # T-053: resolve capability_details for this stage. We
                # prefer the claiming node's row (if claimed) so the
                # description/schema matches the node actually working
                # on it; otherwise fall back to any node advertising
                # the capability.
                claimed_by = r["claimed_by"] if r["claimed_by"] else None
                cap_details = get_capability_details(r["capability"], node_id=claimed_by)
                if cap_details is not None:
                    stage["capability_details"] = cap_details
                task["stages"].append(stage)

            artifact_rows = conn.execute(
                "SELECT artifact_id, name, mime_type, size_bytes, created_by FROM artifacts WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            task["artifacts"] = [
                {
                    "artifact_id": r["artifact_id"],
                    "name": r["name"],
                    "mime_type": r["mime_type"],
                    "size_bytes": r["size_bytes"],
                    "created_by": r["created_by"],
                }
                for r in artifact_rows
            ]

            # T-052: load notes attached to the task (mini-chat).
            note_rows = conn.execute(
                "SELECT id, node_id, message, created_at FROM task_notes "
                "WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
            task["notes"] = [
                {
                    "id": r["id"],
                    "node_id": r["node_id"],
                    "message": r["message"],
                    "created_at": r["created_at"],
                }
                for r in note_rows
            ]
            return task
        finally:
            conn.close()

    @staticmethod
    @_retry_db_write
    def claim_stage(
        node_id: str,
        capability: Optional[str] = None,
        capability_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Claim the next runnable stage for a node.

        If ``capability_type`` is given, only stages whose capability
        matches a capability of that type on this node are considered.
        """
        conn = get_conn()
        try:
            # Determine capabilities of the node if not provided.
            if not capability:
                # Verify the node exists and is approved/online (status
                # gate). The capability names come from the normalized
                # node_capabilities index (T-026) instead of json.loads
                # on the nodes.capabilities TEXT column.
                node_row = conn.execute(
                    "SELECT capabilities FROM nodes WHERE node_id = ? AND status IN ('approved', 'online')",
                    (node_id,),
                ).fetchone()
                if not node_row:
                    return None

                # Use the normalized index for capability names. Fall back
                # to the JSON column if the index is empty (e.g. legacy
                # node that has not heartbeat-synced yet).
                cap_names = get_node_capability_names(node_id)
                if not cap_names:
                    caps = _parse(node_row["capabilities"]) or []
                    if capability_type:
                        caps = [
                            c for c in caps
                            if isinstance(c, dict)
                            and str(c.get("type", "")).lower() == capability_type.lower()
                        ]
                    cap_names = [c["name"] for c in caps if isinstance(c, dict) and c.get("name")]
                    if not cap_names:
                        return None
                elif capability_type:
                    # Filter the normalized index by type via a direct
                    # query — avoids loading the full JSON payload.
                    rows = conn.execute(
                        "SELECT capability_name FROM node_capabilities "
                        "WHERE node_id = ? AND LOWER(capability_type) = LOWER(?)",
                        (node_id, capability_type),
                    ).fetchall()
                    cap_names = [r["capability_name"] for r in rows]
                    if not cap_names:
                        return None
            else:
                cap_names = [capability]

            # Find pending stages whose capability matches and dependencies are completed.
            rows = conn.execute(
                "SELECT * FROM task_stages WHERE status = 'pending' AND capability IN ({}) ORDER BY sequence ASC".format(
                    ",".join("?" for _ in cap_names)
                ),
                cap_names,
            ).fetchall()

            now = _format_time(_now())
            claim_ttl = _format_time(
                _now() + __import__("datetime").timedelta(seconds=settings.claim_ttl_seconds)
            )

            for row in rows:
                deps = _parse(row["depends_on"]) or []
                if deps:
                    completed = conn.execute(
                        "SELECT COUNT(*) FROM task_stages WHERE stage_id IN ({}) AND status = 'completed'".format(
                            ",".join("?" for _ in deps)
                        ),
                        deps,
                    ).fetchone()[0]
                    if completed != len(deps):
                        continue

                # Wenn der Task einen owner_node_id hat, darf nur dieser
                # Node die Stage claimen. Andere Nodes überspringen sie.
                task_owner = conn.execute(
                    "SELECT owner_node_id FROM tasks WHERE task_id = ?",
                    (row["task_id"],),
                ).fetchone()[0]
                if task_owner and task_owner != node_id:
                    continue

                # Claim this stage atomically.
                stage_id = row["stage_id"]
                conn.execute(
                    """
                    UPDATE task_stages
                    SET status = 'claimed', claimed_by = ?, claimed_at = ?, claim_expires_at = ?, updated_at = ?
                    WHERE stage_id = ? AND status = 'pending'
                    """,
                    (node_id, now, claim_ttl, now, stage_id),
                )
                if conn.total_changes == 0:
                    continue

                # Update task status to running if first claim.
                conn.execute(
                    "UPDATE tasks SET status = 'running', updated_at = ? WHERE task_id = ? AND status = 'pending'",
                    (now, row["task_id"]),
                )
                conn.commit()

                event_bus.publish_sync(
                    "stage_claimed",
                    {"task_id": row["task_id"], "stage_id": stage_id, "node_id": node_id},
                )
                stage_dict = _stage_row_to_dict(
                    conn.execute(
                        "SELECT * FROM task_stages WHERE stage_id = ?", (stage_id,)
                    ).fetchone()
                )
                # T-053: attach resolved capability_details so the
                # claiming node sees the description / input_schema of
                # the capability it just claimed, without a second
                # round-trip to the discovery API.
                cap_details = get_capability_details(row["capability"], node_id=node_id)
                if cap_details is not None:
                    stage_dict["capability_details"] = cap_details
                return stage_dict

            return None
        finally:
            conn.close()

    @staticmethod
    @_retry_db_write
    def add_note(task_id: str, node_id: str, message: str) -> Optional[Dict[str, Any]]:
        """Append a task note (T-052 mini-chat between nodes).

        Returns ``{"id", "task_id", "node_id", "message", "created_at"}``
        on success, or ``None`` when the task does not exist.
        """
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if not row:
                return None
            now = _format_time(_now())
            cur = conn.execute(
                "INSERT INTO task_notes (task_id, node_id, message, created_at) "
                "VALUES (?, ?, ?, ?)",
                (task_id, node_id, message, now),
            )
            conn.commit()
            note_id = cur.lastrowid
            return {
                "id": note_id,
                "task_id": task_id,
                "node_id": node_id,
                "message": message,
                "created_at": now,
            }
        finally:
            conn.close()

    @staticmethod
    @_retry_db_write
    def complete_stage(
        stage_id: str, node_id: str, result: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Mark a claimed stage as completed."""
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM task_stages WHERE stage_id = ? AND claimed_by = ? AND status = 'claimed'",
                (stage_id, node_id),
            ).fetchone()
            if not row:
                return None

            now = _format_time(_now())
            conn.execute(
                """
                UPDATE task_stages
                SET status = 'completed', completed_at = ?, result = ?, updated_at = ?
                WHERE stage_id = ?
                """,
                (now, _serialize(result), now, stage_id),
            )

            # Check if all stages completed.
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM task_stages WHERE task_id = ? AND status IN ('pending', 'claimed')",
                (row["task_id"],),
            ).fetchone()[0]
            if pending_count == 0:
                conn.execute(
                    "UPDATE tasks SET status = 'completed', completed_at = ?, updated_at = ? WHERE task_id = ?",
                    (now, now, row["task_id"]),
                )
            conn.commit()

            event_bus.publish_sync(
                "stage_completed",
                {"task_id": row["task_id"], "stage_id": stage_id, "node_id": node_id},
            )
            return _stage_row_to_dict(
                conn.execute("SELECT * FROM task_stages WHERE stage_id = ?", (stage_id,)).fetchone()
            )
        finally:
            conn.close()

    @staticmethod
    @_retry_db_write
    def release_or_fail_claims() -> Dict[str, Any]:
        """Release or fail stages whose claim TTL expired (T-060).

        For each expired claim the stage's ``retry_count`` is
        incremented. When it exceeds ``settings.max_retries`` the stage
        is marked ``failed`` (and the task is failed too if all of its
        stages are failed) instead of being put back to ``pending``,
        which prevents the daemon from reclaiming the same stage forever
        and driving the server into RAM overflow.

        Returns a dict with the stage ids that were released back to
        ``pending`` (``released``), failed (``failed``) and the task ids
        that were failed as a consequence (``tasks_failed``).
        """
        now = _format_time(_now())
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT stage_id, task_id, retry_count FROM task_stages "
                "WHERE status = 'claimed' AND claim_expires_at < ?",
                (now,),
            ).fetchall()
            if not rows:
                return {"released": [], "failed": [], "tasks_failed": []}

            released: List[str] = []
            failed_stage_ids: List[str] = []
            affected_tasks: set[str] = set()

            for row in rows:
                stage_id = row["stage_id"]
                task_id = row["task_id"]
                retry_count = int(row["retry_count"] or 0) + 1
                if retry_count > settings.max_retries:
                    # Retry budget exhausted → fail the stage permanently.
                    conn.execute(
                        """
                        UPDATE task_stages
                        SET status = 'failed', retry_count = ?, claimed_by = NULL,
                            claimed_at = NULL, claim_expires_at = NULL, updated_at = ?
                        WHERE stage_id = ?
                        """,
                        (retry_count, now, stage_id),
                    )
                    failed_stage_ids.append(stage_id)
                    affected_tasks.add(task_id)
                else:
                    # Still within the retry budget → put back to pending.
                    conn.execute(
                        """
                        UPDATE task_stages
                        SET status = 'pending', retry_count = ?, claimed_by = NULL,
                            claimed_at = NULL, claim_expires_at = NULL, updated_at = ?
                        WHERE stage_id = ?
                        """,
                        (retry_count, now, stage_id),
                    )
                    released.append(stage_id)

            tasks_failed = _fail_tasks_if_all_stages_done(
                conn, affected_tasks, now, terminal_stage_status=("failed",)
            )
            conn.commit()

            for stage_id in failed_stage_ids:
                event_bus.publish_sync("stage_failed", {"stage_id": stage_id})
            for task_id in tasks_failed:
                event_bus.publish_sync("task_failed", {"task_id": task_id})

            return {
                "released": released,
                "failed": failed_stage_ids,
                "tasks_failed": tasks_failed,
            }
        finally:
            conn.close()

    @staticmethod
    @_retry_db_write
    def enforce_timeouts() -> Dict[str, List[str]]:
        """Mark claimed stages and their tasks as timed_out when overdue.

        A stage is overdue when ``claimed_at + timeout_seconds < now``.
        When all stages of a task are done/timed_out, the task itself is
        also marked timed_out.

        Returns ``{"stages_timed_out": [...], "tasks_timed_out": [...]}``.
        """
        now = _format_time(_now())
        conn = get_conn()
        try:
            # Find overdue claimed stages.
            overdue = conn.execute(
                """
                SELECT stage_id, task_id FROM task_stages
                WHERE status = 'claimed'
                  AND datetime(claimed_at, '+' || timeout_seconds || ' seconds') < ?
                """,
                (now,),
            ).fetchall()

            timed_out_stages = [r["stage_id"] for r in overdue]
            affected_tasks: set[str] = set()
            tasks_timed_out: List[str] = []

            if timed_out_stages:
                # Mark stages as timed_out.
                conn.execute(
                    """
                    UPDATE task_stages
                    SET status = 'timed_out', updated_at = ?
                    WHERE stage_id IN ({})
                    """.format(",".join("?" for _ in timed_out_stages)),
                    [now] + timed_out_stages,
                )

                # Collect affected task IDs.
                for r in overdue:
                    affected_tasks.add(r["task_id"])

                # For each affected task, check if all stages are done/timed_out.
                for task_id in affected_tasks:
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM task_stages WHERE task_id = ? AND status NOT IN ('completed', 'timed_out')",
                        (task_id,),
                    ).fetchone()[0]
                    if remaining == 0:
                        conn.execute(
                            "UPDATE tasks SET status = 'timed_out', updated_at = ?, completed_at = ? WHERE task_id = ?",
                            (now, now, task_id),
                        )
                        tasks_timed_out.append(task_id)

                conn.commit()

                # Publish events.
                for stage_id in timed_out_stages:
                    event_bus.publish_sync(
                        "stage_timed_out",
                        {"stage_id": stage_id},
                    )
                for task_id in tasks_timed_out:
                    event_bus.publish_sync(
                        "task_timed_out",
                        {"task_id": task_id},
                    )

            return {
                "stages_timed_out": timed_out_stages,
                "tasks_timed_out": tasks_timed_out,
            }
        finally:
            conn.close()


def _fail_tasks_if_all_stages_done(
    conn: sqlite3.Connection,
    task_ids: "set[str]",
    now: str,
    terminal_stage_status: tuple[str, ...],
) -> List[str]:
    """Mark tasks as ``failed`` when every stage is in a terminal state.

    Used by the retry / offline failure paths (T-060, T-061) to fail a
    task as soon as all of its stages have reached a terminal status.
    ``terminal_stage_status`` is the set of stage statuses that count
    as "done" for the purpose of this check (e.g. ``("failed",)`` or
    ``("completed", "failed", "timed_out")``).

    Returns the list of task ids that were transitioned to ``failed``.
    The caller is responsible for committing the transaction and
    publishing the ``task_failed`` events.
    """
    tasks_failed: List[str] = []
    for task_id in task_ids:
        placeholders = ",".join("?" for _ in terminal_stage_status)
        remaining = conn.execute(
            f"SELECT COUNT(*) FROM task_stages "
            f"WHERE task_id = ? AND status NOT IN ({placeholders})",
            (task_id, *terminal_stage_status),
        ).fetchone()[0]
        if remaining == 0:
            conn.execute(
                "UPDATE tasks SET status = 'failed', updated_at = ?, completed_at = ? "
                "WHERE task_id = ? AND status NOT IN ('failed', 'completed', 'timed_out')",
                (now, now, task_id),
            )
            tasks_failed.append(task_id)
    return tasks_failed


def _task_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "task_id": row["task_id"],
        "task_name": row["task_name"],
        "status": row["status"],
        "priority": row["priority"],
        "owner_node_id": row["owner_node_id"],
        "timeout_seconds": row["timeout_seconds"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
    }


def _stage_row_to_dict(row: Any) -> Dict[str, Any]:
    return {
        "stage_id": row["stage_id"],
        "task_id": row["task_id"],
        "stage_name": row["stage_name"],
        "capability": row["capability"],
        "depends_on": _parse(row["depends_on"]),
        "status": row["status"],
        "sequence": row["sequence"],
        "timeout_seconds": row["timeout_seconds"],
        "payload": _parse(row["payload"]),
        "claimed_by": row["claimed_by"],
        "claimed_at": row["claimed_at"],
        "claim_expires_at": row["claim_expires_at"],
        "completed_at": row["completed_at"],
        "result": _parse(row["result"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "retry_count": int(row["retry_count"] or 0),
    }
