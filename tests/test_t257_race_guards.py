"""T-257: race guards — status predicate in every SELECT-based UPDATE (Audit M2/M3/M4/L4).

The class: a watchdog SELECTs candidate rows, then UPDATEs them by bare
``stage_id``. Anything that mutates a row BETWEEN that SELECT and the
UPDATE (a worker completing, a second claimer taking over) was
overwritten — "Zustands-Auferstehung": a COMPLETED stage resurrected to
pending/failed and executed again; a FRESH claim of a second node
stripped by a stale first claimer's tick with no predicate.

Correct pattern (fail_orphaned_stages :1009 precedent): the UPDATE
carries its own status predicate — ``WHERE stage_id = ? AND status =
'pending'`` — so a lost race yields rowcount 0 and NOTHING happens.

Test harness: a connection proxy patched into
``relay_server.core.scheduler.get_conn`` fires a side effect on a SECOND
connection right before the first ``UPDATE task_stages`` of the call —
this deterministically realizes "between SELECT and UPDATE" (the race
window is between two SQL statements on one connection; a worker's own
connection always wins exactly in that window).

L1 (Audit): ``_fail_tasks_if_all_stages_done`` appended unconditionally
(even when its predicate-UPDATE lost the race → task already terminal)
and the callers published ``task_failed``/``status_changed`` with a
HARDCODED old_status ``"running"``. Now: append + publish are gated on
the rowcount and the old_status is the task's TRUE pre-UPDATE status
(SELECT) — T-189 "never guess old_status". Return shape changed
deliberately to ``(task_id, old_status)`` pairs (all three call sites
live in this file); the public watchdog dicts keep id lists.

Semantics kept: claim eligibility, retry policies, event payloads (ids)
and the public watchdog return shapes are unchanged.
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.api.v2.auth import limiter as auth_limiter
from relay_server.config import settings
from relay_server.core import scheduler as sch
from relay_server.core.db import get_conn as real_get_conn
from relay_server.core.db import init_db, q
from relay_server.core.events import event_bus
from relay_server.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test and reset the event-bus history."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t257.db"
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
        auth_limiter.reset()
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


client = TestClient(app)

STALE = "2026-01-01T00:00:00+00:00"
FRESH = "2030-01-01T00:00:00+00:00"


def _seed_node(node_id: str, caps: list[str] | None = None,
               status_value: str = "online") -> None:
    """Direct-DB node seed (test_t247 pattern); task_stages.claimed_by has a FK."""
    c = real_get_conn()
    caps_json = None
    if caps:
        import json as _json

        caps_json = _json.dumps([{"name": name} for name in caps])
    c.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, status, capabilities,"
            " registered_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(node_id) DO NOTHING",
            (node_id, node_id, status_value, caps_json, STALE, STALE),
        )
    )
    c.commit()
    c.close()


def _seed(task_id: str, stage_id: str, *, stage_status: str = "claimed",
          claimed_by: str | None = "W1", retry_count: int = 99,
          claim_expires_at: str = STALE, task_status: str = "running",
          result: str | None = None) -> None:
    """Seed one single-stage task fully controlled (stage + task row)."""
    if claimed_by is not None:
        _seed_node(claimed_by, ["build"])
    _seed_node(task_id, ["build"])  # synthetic owner row keeps the FK happy
    c = real_get_conn()
    c.execute(
        q(
            "INSERT INTO tasks (task_id, task_name, status, priority,"
            " created_at, updated_at) VALUES (?, ?, 'running', 5, ?, ?)"
            " ON CONFLICT(task_id) DO NOTHING",
            (task_id, "t257-" + task_id, STALE, STALE),
        )
    )
    c.execute(
        q("UPDATE tasks SET status = ? WHERE task_id = ?", (task_status, task_id))
    )
    c.execute(
        q(
            "INSERT INTO task_stages (stage_id, task_id, stage_name, capability,"
            " status, sequence, payload, claimed_by, claimed_at,"
            " claim_expires_at, retry_count, result, created_at, updated_at)"
            " VALUES (?, ?, 's1', 'build', ?, 0, '{}', ?, ?, ?, ?, ?, ?, ?)",
            (stage_id, task_id, stage_status, claimed_by, STALE,
             claim_expires_at if stage_status == "claimed" else None,
             retry_count, result, STALE, STALE),
        ),
    )
    c.commit()
    c.close()


def _bump_last_seen(node_id: str, stamp: str = FRESH) -> None:
    """Make a seeded node look ALIVE: the node-dead sweep (third longrun
    rule) uses ``last_seen`` freshness — a stale stamp fails the stage in
    the same tick and buries the assertion."""
    c = real_get_conn()
    c.execute(q("UPDATE nodes SET last_seen = ? WHERE node_id = ?", (stamp, node_id)))
    c.commit()
    c.close()


def _set_longrun_ttl_stale(stage_id: str) -> None:
    """Expire the T-154 longrun lease so the first sweep selects the stage."""
    c = real_get_conn()
    c.execute(
        q("UPDATE task_stages SET longrun_ttl_expires_at = ? WHERE stage_id = ?",
          (STALE, stage_id)),
    )
    c.commit()
    c.close()


def _stage(stage_id: str) -> dict:
    c = real_get_conn()
    row = c.execute(
        q("SELECT status, claimed_by, claimed_at, claim_expires_at, retry_count,"
          " result FROM task_stages WHERE stage_id = ?", (stage_id,)),
    ).fetchone()
    c.close()
    return dict(row)


def _task(task_id: str) -> str:
    c = real_get_conn()
    row = c.execute(q("SELECT status FROM tasks WHERE task_id = ?", (task_id,))).fetchone()
    c.close()
    return row["status"]


def _changes(etype: str) -> list:
    return [e for e in event_bus.recent(200) if e.get("type") == etype]


def _interleave_get_conn(mark: str, action):
    """Patch target for ``scheduler.get_conn``: run ``action`` on a second
    connection right before the first mark-matching UPDATE of the body —
    the deterministic "between SELECT and UPDATE" window."""
    fired = {"done": False}

    class _Proxy:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **k):
            text = str(sql)
            if not fired["done"] and mark in text and "UPDATE" in text.upper():
                fired["done"] = True
                c2 = real_get_conn()
                try:
                    action(c2)
                    c2.commit()
                finally:
                    c2.close()
            return self._real.execute(sql, *a, **k)

        def commit(self):
            return self._real.commit()

        def close(self):
            return self._real.close()

        def __getattr__(self, name):
            return getattr(self._real, name)

    def _factory():
        return _Proxy(real_get_conn())

    return _factory


def _complete_now(stage_id: str):
    """Interleaved worker win: the stage completes with a result."""
    def action(c2):
        c2.execute(
            q(
                "UPDATE task_stages SET status = 'completed', completed_at = ?,"
                " result = '{\"ok\": true}' WHERE stage_id = ?",
                (STALE, stage_id),
            ),
        )
    return action


def _reclaim_as(stage_id: str, node_id: str):
    """Interleaved second-claimer win: a FRESH claim overwrites the stale one."""
    def action(c2):
        c2.execute(
            q(
                "UPDATE task_stages SET status = 'claimed', claimed_by = ?,"
                " claimed_at = ?, claim_expires_at = ?, retry_count = 0"
                " WHERE stage_id = ?",
                (node_id, FRESH, FRESH, stage_id),
            ),
        )
    return action


# ── T3: terminal stage vs watchdog tick — no resurrection ───────────


def test_watchdog_tick_does_not_resurrect_completed_stage():
    """T3: the worker completes the stage between the watchdog's SELECT
    and the fail/release UPDATE — the tick must NOT resurrect it.

    RED: the UPDATE had no status predicate → the completed stage was
    overwritten (failed or re-pended) and the claim fields stripped.
    """
    _seed("tA", "stA")
    from unittest import mock

    with mock.patch.object(sch, "get_conn", _interleave_get_conn(
            "task_stages", _complete_now("stA"))):
        result = sch.Scheduler.release_or_fail_claims()

    row = _stage("stA")
    assert row["status"] == "completed", (
        "a completed stage must never resurrect via the watchdog"
    )
    assert row["result"] == '{"ok": true}', "the result survives untouched"
    assert row["claimed_by"] == "W1", "the completed row keeps its claim provenance"
    assert result["released"] == [] and result["failed"] == [], (
        "a lost race is reported as no-op"
    )
    assert _changes("status_changed") == [], "no events for a no-op tick"


def test_watchdog_tick_releases_only_stably_claimed_stage():
    """Non-race happy path keeps working: an expired, still-claimed stage is
    released within the retry budget (regression lock next to the guards)."""
    _seed("tB", "stB", retry_count=0)
    result = sch.Scheduler.release_or_fail_claims()
    assert result["released"] == ["stB"]
    assert _stage("stB")["status"] == "pending"


def test_watchdog_tick_does_not_strip_a_fresh_second_claim():
    """T5/M3-class: a second node re-claims the expired stage between the
    watchdog's SELECT and UPDATE — the fresh claim must survive.

    RED: the bare stage_id UPDATE released/failed the fresh claimer's row.
    """
    _seed("tC", "stC", claimed_by="WOLD", retry_count=0)  # release path
    _seed_node("WNEW", ["build"])
    from unittest import mock

    with mock.patch.object(sch, "get_conn", _interleave_get_conn(
            "task_stages", _reclaim_as("stC", "WNEW"))):
        result = sch.Scheduler.release_or_fail_claims()

    row = _stage("stC")
    assert row["status"] == "claimed", "the fresh claim survives the stale tick"
    assert row["claimed_by"] == "WNEW"
    assert row["claimed_at"] == FRESH
    assert result["released"] == [] and result["failed"] == []


def test_second_claimer_does_not_overwrite_existing_claim():
    """M3 regression lock: once a stage is claimed, a second claimer gets
    nothing (the claim UPDATE's status predicate + candidate filter)."""
    _seed_node("W1", ["build"])
    _seed_node("W2", ["build"])
    _seed("tD", "stD", stage_status="pending", claimed_by=None, retry_count=0)
    first = sch.Scheduler.claim_stage("W1", capability="build")
    assert first is not None and first["claimed_by"] == "W1"
    second = sch.Scheduler.claim_stage("W2", capability="build")
    assert second is None, "a claimed stage is not re-claimed by another node"
    assert _stage("stD")["claimed_by"] == "W1"


# ── complete_stage race (M4 class) ───────────────────────────────────


def test_complete_stage_race_lost_reports_nothing():
    """complete_stage: the claim is taken away between its SELECT and its
    UPDATE (watchdog failed the stage) — the complete must NOT land: no
    events, stage stays failed, task not completed.

    RED: the bare stage_id UPDATE completed the failed stage (M4 class).
    """
    _seed("tE", "stE", claim_expires_at=STALE)

    def _fail_now(c2):
        c2.execute(
            q("UPDATE task_stages SET status = 'failed', updated_at = ? WHERE stage_id = ?",
              (STALE, "stE")),
        )

    from unittest import mock

    with mock.patch.object(sch, "get_conn", _interleave_get_conn("task_stages", _fail_now)):
        outcome = sch.Scheduler.complete_stage("stE", "W1", result={"ok": False})

    assert outcome is None, "a raced-away claim cannot be completed"
    row = _stage("stE")
    assert row["status"] == "failed", "the concurrent failure wins"
    assert _task("tE") != "completed"
    assert _changes("stage_completed") == [], "no stage_completed for a lost race"
    assert _changes("task_completed") == [], "no task_completed for a lost race"

    # Non-race happy path still completes.
    _seed("tF", "stF", claim_expires_at=FRESH)
    ok = sch.Scheduler.complete_stage("stF", "W1", result={"ok": True})
    assert ok is not None and ok["status"] == "completed"
    assert _task("tF") == "completed"


# ── orphan watchdog + deleted-node sweep — same predicate class ──────


def test_orphan_fail_tick_does_not_resurrect_completed_stage():
    """fail_orphaned_stages already carries the :1009 predicate — the race
    is now also SILENT (no lying events for a row the UPDATE lost).

    RED: rowcount 0 but the stage id was appended and
    pending→failed status_changed events were published for a COMPLETED
    stage."""
    # A stage whose capability nobody advertises + completed in the race
    # window. Seed pending → the SELECT sees it; the interleaved complete
    # makes the UPDATE lose.
    _seed("tG", "stG", stage_status="pending", claimed_by=None, retry_count=0)
    from unittest import mock

    with mock.patch.object(sch, "get_conn", _interleave_get_conn(
            "task_stages", _complete_now("stG"))):
        result = sch.Scheduler.fail_orphaned_stages()

    assert _stage("stG")["status"] == "completed"
    assert result["stages_failed"] == [], "a lost race is not a failed stage"
    assert _changes("stage_failed") == []
    # The truly-unadvertised pending sibling still fails (happy path intact).
    _seed("tH", "stH", stage_status="pending", claimed_by=None, retry_count=0)
    result = sch.Scheduler.fail_orphaned_stages()
    assert "stH" in result["stages_failed"]
    assert _stage("stH")["status"] == "failed"


def test_fail_stages_of_deleted_node_does_not_resurrect_completed_stage():
    """The node-delete sweep (F-19) had the bare stage_id UPDATE: a stage
    completed between its SELECT and UPDATE resurrected to failed. The
    claimed-predicate keeps terminal stages final.
    """
    _seed("tI", "stI")
    from unittest import mock

    with mock.patch.object(sch, "get_conn", _interleave_get_conn(
            "task_stages", _complete_now("stI"))):
        from relay_server.core.scheduler import fail_stages_of_deleted_node

        proxy = _interleave_get_conn("task_stages", _complete_now("stI"))()
        failed = fail_stages_of_deleted_node(proxy, "W1", STALE)
        proxy.commit()
        proxy.close()

    assert failed == [], "a completed stage is not re-failed"
    row = _stage("stI")
    assert row["status"] == "completed"
    assert row["result"] == '{"ok": true}'
    assert _task("tI") != "failed"


def test_fail_stages_of_deleted_node_happy_path():
    _seed("tJ", "stJ", claim_expires_at=FRESH)
    from relay_server.core.scheduler import fail_stages_of_deleted_node

    c = real_get_conn()
    failed = fail_stages_of_deleted_node(c, "W1", STALE)
    c.commit()
    c.close()
    # The caller owns the txn — assert AFTER the commit.
    assert failed == ["stJ"]
    assert _stage("stJ")["status"] == "failed"
    assert _task("tJ") == "failed"  # linear policy: the critical helper fires


# ── L1: event gate + true old_status ─────────────────────────────────


def _call_all_done(task_id: str, terminal=("failed",)):
    c = real_get_conn()
    out = sch._fail_tasks_if_all_stages_done(c, {task_id}, STALE, terminal)
    c.commit()
    c.close()
    return out


def test_all_done_helper_reports_true_old_status():
    """T-189 'never guess old_status': the helper rides (id, old_status)
    pairs — the task's TRUE pre-UPDATE status, not a hardcoded 'running'."""
    _seed("tK", "stK", stage_status="failed", task_status="accepted",
          retry_count=99, claim_expires_at=STALE)
    out = _call_all_done("tK")
    assert out == [("tK", "accepted")], (
        "the real pre-status is reported (accepted — 'running' would be a guess)"
    )


def test_all_done_helper_rowcount_gate_no_double_fail_event():
    """L1: a task that is ALREADY terminal is not re-appended — no
    duplicate task_failed / no lying 'running'→'failed' event.

    RED: the append fired unconditionally (rowcount was ignored).
    """
    _seed("tL", "stL", stage_status="failed", task_status="failed", retry_count=99)
    out = _call_all_done("tL")
    assert out == [], "an already-failed task is not re-failed (rowcount gate)"
    # And through the watchdog publish path: no duplicate/lying events.
    _seed("tM", "stM", stage_status="failed", task_status="failed", retry_count=99)
    result = sch.Scheduler.release_or_fail_claims()
    assert result["tasks_failed"] == []
    assert _changes("task_failed") == [], "no redundant task_failed event"
    assert _changes("status_changed") == []


def test_watchdog_old_status_is_the_true_pre_status():
    """Through the real watchdog path, task level events carry the task's
    true old_status (regression lock — the linear-failure helper publishes
    it already; the L1 gate keeps the all-done path from lying, see the
    rowcount-gate test)."""
    _seed("tN", "stN", stage_status="claimed", task_status="accepted",
          retry_count=99, claim_expires_at=STALE)
    result = sch.Scheduler.release_or_fail_claims()
    assert "tN" in result["tasks_failed"]
    task_changes = [
        e for e in _changes("status_changed")
        if e.get("payload", {}).get("entity_type") == "task"
    ]
    assert task_changes, "the task event is published"
    assert task_changes[-1]["payload"]["old_status"] == "accepted", (
        f"true old_status, not 'running' — got {task_changes[-1]['payload']}"
    )
    assert task_changes[-1]["payload"]["new_status"] == "failed"


def test_longrun_leases_do_not_resurrect_and_publish_only_real_changes():
    """enforce_longrun_leases: the predicate + rowcount gate keep a stage
    completed in the race window terminal (and silent); the real
    accepted→orphaned transition stays loud."""
    # Race: between SELECT and the first UPDATE the stage completes.
    _seed("tO", "stO", stage_status="accepted", claimed_by="W1", retry_count=0)
    _bump_last_seen("W1")
    _set_longrun_ttl_stale("stO")
    from unittest import mock

    with mock.patch.object(sch, "get_conn", _interleave_get_conn(
            "task_stages", _complete_now("stO"))):
        result = sch.Scheduler.enforce_longrun_leases()

    assert _stage("stO")["status"] == "completed", "completed stays terminal"
    orphaned = [e for e in _changes("status_changed")
                if e.get("payload", {}).get("entity_id") == "stO"]
    assert orphaned == [], "no lying accepted→orphaned event for a lost race"
    assert result["accepted_orphaned"] == []

    # Happy path (no race): the lease expiry still moves accepted→orphaned.
    # The owner must look ALIVE (fresh last_seen) or the third sweep
    # (node-dead > grace) would fail the orphaned stage in the same tick.
    _seed("tP", "stP", stage_status="accepted", claimed_by="W1", retry_count=0)
    _bump_last_seen("W1")
    c = real_get_conn()
    # Expire the T-154 longrun lease (sweep 1) while keeping the NOTE
    # fresh — a NULL/old last_note_at is instant-dead for sweep 2
    # (orphaned > 24h) IN THE SAME TICK and would bury the assertion.
    c.execute(
        q("UPDATE task_stages SET longrun_ttl_expires_at = ?, last_note_at = ?"
          " WHERE stage_id = ?", (STALE, FRESH, "stP")),
    )
    c.commit()
    c.close()
    result = sch.Scheduler.enforce_longrun_leases()
    assert "stP" in result["accepted_orphaned"]
    assert _stage("stP")["status"] == "orphaned"