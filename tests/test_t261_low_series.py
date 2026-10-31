"""T-261: LOW series — L2 q() strictness, L3 dead-pin sweep in maintenance,
L5 publish-after-commit, L6 status-list bindparams, ConfigDict (V3 prep).

Findings this suite pins:

* **L2** — ``core.db.q()`` silently swallowed surplus/missing positional
  params via ``zip`` truncation; a mismatch is a caller bug and must be
  loud (ValueError at call time, not a silently wrong bind or a delayed
  unbound-bindparam error).
* **L3** — the T-246 dead-pin sweep (pending stage pinned to a vanished
  node → failed) ran INSIDE ``claim_stage``: every claimant paid the write.
  Moved onto ``orphaned_stage_cleanup`` (``fail_orphaned_stages``); the
  claim loop is read-only again — a claimer now sees the pinned row simply
  unclaimable (pin gate), the maintenance sweep fails it.
* **L5** — ``complete_stage`` published ``task_completed``/``status_changed``
  BEFORE ``conn.commit()``: subscribers saw a state the DB had not yet
  committed (a concurrent reader on another connection could disagree with
  the event). Both publishes moved below the commit.
* **L6** — ``_lookup_route`` interpolated the live-status list as quoted
  f-string literals into SQL; it now binds via ``q()`` bindparams.
* **ConfigDict** — ``Settings`` moved off the Pydantic-V1 style
  ``class Config:`` (deprecated since V2.0, REMOVED in V3.0) to
  ``SettingsConfigDict``.
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.main import app  # noqa: E402  (first — establishes the import graph)

from relay_server.config import Settings, settings  # noqa: E402
from relay_server.core.db import get_conn, init_db, q  # noqa: E402
from relay_server.core.events import event_bus  # noqa: E402
from relay_server.core import route_registry as rr  # noqa: E402
from relay_server.core import scheduler as sched_mod  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t261.db"
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


client = TestClient(app, raise_server_exceptions=False)


def _plant_node(node_id: str = "n-t261", endpoint: str = "http://node.test:9201"):
    now = "2026-01-01T00:00:00+00:00"
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, status, endpoint, last_seen, registered_at)"
            " VALUES (?, ?, 'online', ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (node_id, node_id, endpoint),
        )
    )
    conn.commit()
    conn.close()
    return node_id


def _plant_task_and_stage(
    task_id: str = "t-t261",
    stage_id: str = "st-t261",
    status: str = "claimed",
    node_id: str | None = "n-t261",
    target_node_id: str | None = None,
):
    now = "2026-01-01T00:00:00+00:00"
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO tasks (task_id, task_name, status, created_at, updated_at)"
            " VALUES (?, 't261-task', ?, ?, ?)",
            (task_id, "pending" if status == "pending" else "running", now, now),
        )
    )
    conn.execute(
        q(
            "INSERT INTO task_stages (stage_id, task_id, stage_name, capability, status,"
            " sequence, created_at, updated_at, claimed_by, target_node_id)"
            " VALUES (?, ?, 's1', 'cap.t261', ?, 0, ?, ?, ?, ?)",
            (stage_id, task_id, status, now, now, node_id, target_node_id),
        )
    )
    conn.commit()
    conn.close()


def _stage_status(stage_id: str = "st-t261") -> str:
    conn = get_conn()
    row = conn.execute(
        q("SELECT status FROM task_stages WHERE stage_id = ?", (stage_id,))
    ).fetchone()
    conn.close()
    return row["status"]


# ── L2: q() parameter strictness ──────────────────────────────────────────


def test_q_rejects_surplus_params():
    """More params than '?' must raise, not be silently truncated away."""
    with pytest.raises(ValueError, match="arity"):
        q("SELECT x FROM nodes WHERE node_id = ?", ("a", "b", "c"))


def test_q_rejects_params_without_placeholders():
    with pytest.raises(ValueError, match="arity"):
        q("SELECT x FROM nodes", ({"node_id": "a"},))


def test_q_rejects_missing_params():
    with pytest.raises(ValueError, match="arity"):
        q("SELECT x FROM nodes WHERE node_id = ? AND status = ?", ("a",))


def test_q_still_accepts_matching_params_and_omitted_params():
    conn = get_conn()
    try:
        bound = q("SELECT ? AS v", ("ok",))
        assert getattr(bound, "text", None) == "SELECT :p0 AS v"
        assert conn.execute(bound).fetchone()["v"] == "ok"
        assert conn.execute(q("SELECT 41+1 AS v")).fetchone()["v"] == 42
    finally:
        conn.close()


# ── L3: dead-pin sweep moved into the maintenance scheduler ───────────────


def test_dead_pin_stays_pending_on_claim_and_fails_in_maintenance():
    """T-246 semantics kept: dead-pinned rows are unclaimable (pin gate).
    CHANGED by L3: claim_stage no longer WRITES the fail — the row stays
    pending until orphaned_stage_cleanup's sweep fails it. The per-claim
    UPDATE (every claimant paying a write) is gone."""
    import sqlite3

    node_id = _plant_node()
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO node_capabilities (node_id, capability_name, capability_version, available, updated_at)"
            " VALUES (?, 'cap.t261', '1.0.0', 1, '2026-01-01T00:00:00+00:00')",
            (node_id,),
        )
    )
    conn.commit()
    conn.close()

    # Task + dead-pinned stage planted on a raw connection (FK pragma OFF —
    # the orphan-row shape the live DB carries; test_t246 pattern).
    raw = sqlite3.connect(str(settings.db_path))
    raw.row_factory = sqlite3.Row
    raw.execute(
        "INSERT INTO tasks (task_id, task_name, status, created_at, updated_at)"
        " VALUES ('t-deadpin', 't261-task', 'pending',"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO task_stages (stage_id, task_id, stage_name, capability, status,"
        " sequence, created_at, updated_at, target_node_id)"
        " VALUES ('st-deadpin', 't-deadpin', 's1', 'cap.t261', 'pending', 0,"
        " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'DEADNODE')"
    )
    raw.commit()
    raw.close()

    claimed = sched_mod.Scheduler.claim_stage(node_id, "cap.t261")
    assert claimed is None, "a dead-pinned stage must never be claimed"
    assert _stage_status("st-deadpin") == "pending", (
        "L3: the claim loop must not write the fail anymore"
    )

    result = sched_mod.Scheduler.fail_orphaned_stages()
    assert result.get("dead_pinned_failed") == ["st-deadpin"]
    assert _stage_status("st-deadpin") == "failed", (
        "the maintenance sweep fails the dead-pinned pending stage"
    )


def test_orphaned_stage_cleanup_registered_with_new_sweep():
    """The sweep really rides the orphaned_stage_cleanup maintenance task."""
    from relay_server.core.maintenance import maintenance_scheduler

    maintenance_scheduler.register_defaults()
    names = [t["name"] for t in maintenance_scheduler.status()]
    assert "orphaned_stage_cleanup" in names


# ── L5: complete_stage publishes after commit ─────────────────────────────


def test_task_completed_publish_sees_committed_state(monkeypatch):
    """The task_completed subscriber must observe the COMMITTED task state:
    a second connection reads the DB at publish time."""
    _plant_node()
    _plant_task_and_stage()  # single stage, claimed by n-t261
    seen_at_publish = []
    orig = sched_mod.event_bus.publish_sync

    def spy(event, payload, *a, **kw):
        if event == "task_completed":
            second = get_conn()
            row = second.execute(
                q("SELECT status FROM tasks WHERE task_id = ?", (payload["task_id"],))
            ).fetchone()
            second.close()
            seen_at_publish.append(row["status"])
        orig(event, payload, *a, **kw)

    monkeypatch.setattr(sched_mod.event_bus, "publish_sync", spy)
    result = sched_mod.Scheduler.complete_stage("st-t261", "n-t261", {"ok": 1})

    assert result is not None
    assert seen_at_publish == ["completed"], (
        "L5: task_completed must fire AFTER the commit — subscribers must "
        "never observe a pre-commit state"
    )


# ── L6: _lookup_route binds the status list ───────────────────────────────


def test_lookup_route_binds_status_list(monkeypatch):
    """The live-status list must travel as bindparams, not as quote-wrapped
    f-string literals: a status value carrying a quote must not break (or
    inject into) the SQL."""
    node_id = _plant_node("n-t261-quoted")
    conn = get_conn()
    conn.execute(
        q("UPDATE nodes SET status = 'o''ney' WHERE node_id = ?", (node_id,))
    )
    conn.commit()
    conn.execute(
        q(
            "INSERT INTO node_routes (node_id, path, method, auth, upstream)"
            " VALUES (?, '/r', 'GET', 'none', 'http://node.test:9201/r')",
            (node_id,),
        )
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(rr, "node_live_statuses", lambda: ["o'ney"])
    found = rr._lookup_route(node_id, "/r", "GET")
    assert found is not None, "bindparam status list resolves the route (no quoting f-string)"


# ── Pydantic V3 prep: ConfigDict ──────────────────────────────────────────


def test_settings_no_v1_style_class_config():
    """`class Config:` is removed in Pydantic V3 — Settings must use
    SettingsConfigDict under V2, with the same keys."""
    assert not hasattr(Settings, "Config"), (
        "V3 prep: Settings must not carry a V1-style class-based Config"
    )
    assert Settings.model_config.get("env_prefix") == "RELAY_"
    assert Settings.model_config.get("extra") == "ignore"