"""SF-Findings (Wiederaudit 2026-10-31): regression tests.

The class: predicate-less UPDATEs (and their consequences) let lost
races / already-terminal rows be overwritten — "Zustands-Auferstehung"
on the task level. SF3 lives in scheduler.complete_stage, SF4/SF5 in
discovery.mark_offline_nodes; each test pins the pre-fix behaviour RED
and the fixed behaviour GREEN (audit evidence in the commit bodies).

SF3 (audit 2026-10-31): the task-level UPDATE in complete_stage ran
without a status predicate/rowcount gate — a linear-failed task was
resurrected to 'completed' when its last stage completed (event:
status_changed old_status=failed → new_status=completed). Fix: the
UPDATE carries a terminal-status predicate; rowcount 0 → no task
completion, no task-level events (T-257 pattern: never resurrect a
lost race).
"""

import os
import tempfile
from pathlib import Path

import pytest

os.environ["RELAY_DB_PATH"] = ""

from relay_server.config import settings
from relay_server.core import scheduler as sch
from relay_server.core.db import get_conn as real_get_conn
from relay_server.core.db import init_db, q
from relay_server.core.events import event_bus


STALE = "2026-01-01T00:00:00+00:00"
FRESH = "2030-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test and reset the event-bus history."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "sf_audit.db"
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


def _seed_node(node_id: str, status_value: str = "online", last_seen: str = STALE) -> None:
    """Direct-DB node seed (test_t257 pattern); task_stages.claimed_by has a FK."""
    c = real_get_conn()
    caps_json = '[{"name": "build"}]'
    c.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, status, capabilities,"
            " registered_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(node_id) DO NOTHING",
            (node_id, node_id, status_value, caps_json, STALE, last_seen),
        )
    )
    c.commit()
    c.close()


def _seed_task(task_id: str, status: str = "running") -> None:
    _seed_node(task_id)  # synthetic owner row keeps the FK happy
    c = real_get_conn()
    c.execute(
        q(
            "INSERT INTO tasks (task_id, task_name, status, priority,"
            " created_at, updated_at) VALUES (?, ?, ?, 5, ?, ?)"
            " ON CONFLICT(task_id) DO NOTHING",
            (task_id, "sf-" + task_id, status, STALE, STALE),
        )
    )
    c.commit()
    c.close()


def _seed_stage(task_id: str, stage_id: str, *, stage_status: str = "claimed",
                claimed_by: str | None = "W1", retry_count: int = 0,
                result: str | None = None) -> None:
    c = real_get_conn()
    if stage_status == "claimed":
        c.execute(
            q(
                "INSERT INTO task_stages (stage_id, task_id, stage_name, capability,"
                " status, sequence, payload, claimed_by, claimed_at,"
                " claim_expires_at, retry_count, result, created_at, updated_at)"
                " VALUES (?, ?, 's1', 'build', ?, 0, '{}', ?, ?, ?, ?, ?, ?, ?)",
                (stage_id, task_id, stage_status, claimed_by, STALE, FRESH,
                 retry_count, result, STALE, STALE),
            )
        )
    else:
        c.execute(
            q(
                "INSERT INTO task_stages (stage_id, task_id, stage_name, capability,"
                " status, sequence, payload, claimed_by, claimed_at,"
                " claim_expires_at, retry_count, result, created_at, updated_at)"
                " VALUES (?, ?, 's1', 'build', ?, 0, '{}', NULL, NULL, NULL, ?, ?, ?, ?)",
                (stage_id, task_id, stage_status, retry_count, result, STALE, STALE),
            )
        )
    c.commit()
    c.close()


def _seed(task_id: str, stage_id: str, *, stage_status: str = "claimed",
          claimed_by: str | None = "W1", retry_count: int = 0,
          task_status: str = "running", result: str | None = None) -> None:
    """One single-stage task, fully controlled (task + stage rows)."""
    if claimed_by is not None:
        _seed_node(claimed_by)
    _seed_task(task_id, task_status)
    _seed_stage(task_id, stage_id, stage_status=stage_status,
                claimed_by=claimed_by, retry_count=retry_count, result=result)


def _task(task_id: str) -> str:
    c = real_get_conn()
    row = c.execute(q("SELECT status FROM tasks WHERE task_id = ?", (task_id,))).fetchone()
    c.close()
    return row["status"]


def _task_row(task_id: str) -> dict:
    c = real_get_conn()
    row = c.execute(
        q("SELECT status, completed_at, updated_at FROM tasks WHERE task_id = ?", (task_id,))
    ).fetchone()
    c.close()
    return dict(row)


def _stage(stage_id: str) -> dict:
    c = real_get_conn()
    row = c.execute(
        q("SELECT status, claimed_by, result FROM task_stages WHERE stage_id = ?", (stage_id,))
    ).fetchone()
    c.close()
    return dict(row)


def _changes(etype: str) -> list:
    return [e for e in event_bus.recent(200) if e.get("type") == etype]


def _task_status_changes() -> list:
    return [e for e in _changes("status_changed")
            if e.get("payload", {}).get("entity_type") == "task"]


# ── SF3: complete_stage — task-level UPDATE needs the status predicate ──


def test_sf3_complete_stage_never_resurrects_a_linear_failed_task():
    """A linear-failed task must stay failed when its last stage completes.

    Seeded exactly like the audited race: stage 1 exhauster ('failed',
    linear failure policy already fired → task is 'failed'), stage 2 is
    still claimed and gets completed now. Pre-fix, the task-level UPDATE
    (no status predicate) flipped the task to 'completed' — the failed
    state resurrected (status_changed: failed→completed).
    """
    _seed_node("W1")  # claimer row (FK on task_stages.claimed_by)
    _seed_task("tR", "failed")  # linear failure already fired
    _seed_stage("tR", "stR1", stage_status="failed", retry_count=99)
    _seed_stage("tR", "stR2", stage_status="claimed", claimed_by="W1")

    out = sch.Scheduler.complete_stage("stR2", "W1", {"ok": True})
    assert out is not None, "the stage complete itself must land (its own T-257 predicate holds)"
    assert _stage("stR2")["status"] == "completed"

    assert _task("tR") == "failed", "a failed task is never resurrected to completed"
    row = _task_row("tR")
    assert row["completed_at"] is None, "no completed_at stamp for a failed task"

    assert _changes("task_completed") == [], "no task_completed event for a resurrected task"
    task_events = _task_status_changes()
    assert task_events == [], (
        f"no lying failed→completed task event — got {task_events}"
    )


def test_sf3_complete_stage_task_completed_happy_path_intact():
    """The predicate must not over-block: a normal task (running) completes
    normally, with the task-level events it has always fired."""
    _seed("tH", "stH", stage_status="claimed", task_status="running")
    out = sch.Scheduler.complete_stage("stH", "W1", {"ok": True})
    assert out is not None
    assert _task("tH") == "completed"
    assert len(_changes("task_completed")) == 1
    task_events = _task_status_changes()
    assert task_events[-1]["payload"]["old_status"] == "running"
    assert task_events[-1]["payload"]["new_status"] == "completed"

# ── SF4: offline sweep — SELECT-based stage UPDATE needs the predicate ──


def _interleave_get_conn(action, match: str = "UPDATE task_stages"):
    """Patch target for ``discovery.get_conn`` (test_t257 pattern): the
    proxy fires ``action`` right before the first statement matching
    ``match`` — by default the sweep's stage-fail UPDATE, i.e. after the
    ``status = 'claimed'`` SELECT has returned and ``offline_ids`` is
    determined: the deterministic "between SELECT and UPDATE" window the
    SF4 predicate must protect.

    SQLite is single-writer, so the second-connection write of test_t257
    is not available here: the sweep holds the only write transaction
    while it runs; ``action`` is executed on the proxy's own connection
    — the row state the predicated UPDATE must cope with is identical."""
    fired = {"done": False}

    def _factory():
        real = real_get_conn()

        class _Proxy:
            def execute(self, sql, *a, **k):
                text = str(sql)
                if not fired["done"] and match in text:
                    fired["done"] = True
                    action(real)
                    # SQLite: the sweep holds one write transaction for the
                    # whole call — the action's statement only becomes
                    # visible after it commits. The winning transactor of
                    # the audited races has its commit, so has the proxy's.
                    real.commit()
                return real.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(real, name)

        return _Proxy()

    return _factory


def _complete_now(stage_id: str):
    """Winning worker: the stage completes with its result between the
    sweep's SELECT and its stage-fail UPDATE."""
    def action(conn):
        conn.execute(
            q("UPDATE task_stages SET status = 'completed', completed_at = ?,"
              " result = '{\"ok\": true}' WHERE stage_id = ?",
              (STALE, stage_id)),
        )

    return action


def test_sf4_offline_sweep_never_fails_a_concurrently_completed_stage():
    """SF4 (audit 2026-10-31): mark_offline_nodes SELECTs claimed stages of
    dead nodes, then UPDATEd each by bare ``stage_id`` — a stage the worker
    completed BETWEEN the SELECT and the sweep's UPDATE was flipped to
    'failed': the worker's result was buried and the task was failed by the
    remaining==0 branch (T-257's "in every SELECT-based stage UPDATE" claim
    held only for scheduler.py; discovery.mark_offline_nodes was missed).
    Fix: the UPDATE carries ``AND status = 'claimed'`` and the appends are
    gated on rowcount — a lost race yields rowcount 0 and NOTHING happens.
    """
    from unittest import mock

    from relay_server.core import discovery as disc

    # W1 seeded stale last_seen + online → offline candidate; its claimed
    # stage stC completes in the SELECT→UPDATE window.
    _seed("tC", "stC", stage_status="claimed", claimed_by="W1", task_status="running")
    factory = _interleave_get_conn(_complete_now("stC"))
    with mock.patch.object(disc, "get_conn", factory):
        offline = disc.mark_offline_nodes()

    assert "W1" in offline, "the node itself still went offline (the sweep ran)"
    assert _stage("stC")["status"] == "completed", "a completed stage is never re-failed by the sweep"
    assert _stage("stC")["result"] == '{"ok": true}', "the worker's result survives"
    assert _stage("stC")["claimed_by"] == "W1", "the claim is not stripped from a completed stage"
    assert _changes("stage_failed") == [], "no lying stage_failed event for a lost race"
    assert _changes("task_failed") == [], "no task_failed caused by a buried result"
    assert _task("tC") == "running", "the task is not failed off its completed stage"


def test_sf4_offline_sweep_happy_path_still_fails_a_claimed_stage():
    """The predicate must not over-block: a genuinely dead node's claimed
    stage is still failed by the sweep (events + task failure intact)."""
    from relay_server.core import discovery as disc

    _seed("tD", "stD", stage_status="claimed", claimed_by="W2", task_status="running")
    offline = disc.mark_offline_nodes()

    assert "W2" in offline
    assert _stage("stD")["status"] == "failed"
    assert _stage("stD")["claimed_by"] is None, "the dead claim is released"
    assert _task("tD") == "failed"
    assert len(_changes("stage_failed")) == 1
