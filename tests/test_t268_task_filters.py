"""T-268: server-side query filters on ``GET /relay/v2/scheduler/tasks``.

Ist state (verified on main be943fc): the endpoint accepted ONLY a single
``status`` value and silently IGNORED unknown query params — an admin asking
``?submitted_by=<id>`` received the complete list with HTTP 200 (board T-268:
"1.364 Tasks, Filter nur client-seitig moeglich"). That silent ignoring is
exactly the finding this task fixes.

Soll (plan 2026-11-12_t268-task-filters.md):
- ``status`` — comma-separated list (single value stays valid); unknown
  status value -> 422 (fail loud).
- ``submitted_by`` / ``owner_node_id`` — AND filters; admin may use any id,
  non-admin nodes may only pass their OWN id (deviation -> 400, never a
  silent rewrite).
- ``created_since`` / ``created_until`` — ISO-8601 bounds, naive input is
  treated as UTC, invalid input -> 422, comparison happens in SQL against
  the lexicographic ``created_at`` ISO string.
- All filters AND-combine and only NARROW the T-227 read scope
  (admin OR owner OR sender) — they never widen it.
- Scope clause of ``Scheduler.list_tasks`` stays untouched: plain non-admin
  listings keep the T-005g fan-out contract (owner-owned OR sender OR
  ownerless rows).

Test shape follows tests/test_t229_priority_claim.py (fresh_db autouse
fixture with a temp database, TestClient, register/approve chains; rows are
seeded directly so each filter's exact row set is deterministic).
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.api.v2.auth import limiter as auth_limiter
from relay_server.config import settings
from relay_server.core.auth import generate_secret, hash_secret
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.core.scheduler import Scheduler
from relay_server.main import app

BASE = "/relay/v2/scheduler"

# ── canonical world matrix ────────────────────────────────────────
#
# | name                  | status    | owner   | submitted | created (UTC)     |
# |-----------------------|-----------|---------|-----------|-------------------|
# | t268-snd-a-running    | running   | NODE_B  | NODE_A    | 2026-11-01T08:00  |
# | t268-own-a-completed  | completed | NODE_A  | NODE_A    | 2026-11-02T09:30  |
# | t268-own-a-pending    | pending   | NODE_A  | NODE_A    | 2026-11-03T07:15  |
# | t268-free-own-a-failure | failed    | NULL    | NODE_A    | 2026-11-03T23:59  |
# | t268-own-a-cancelled  | cancelled | NODE_A  | NODE_A    | 2026-11-04T12:00  |
# | t268-snd-b-pending    | pending   | NODE_B  | NODE_B    | 2026-11-05T16:45  |
# | t268-free-ownerless   | pending   | NULL    | NULL      | 2026-11-06T08:00  |
# | t268-win-a-pending    | pending   | NODE_A  | NODE_A    | 2026-11-06T18:00  |

ALL_NAMES = {
    "t268-snd-a-running",
    "t268-own-a-completed",
    "t268-own-a-pending",
    "t268-free-own-a-failure",
    "t268-own-a-cancelled",
    "t268-snd-b-pending",
    "t268-free-ownerless",
    "t268-win-a-pending",
}
SUBMITTED_A = ALL_NAMES - {"t268-snd-b-pending", "t268-free-ownerless"}
SUBMITTED_B = {"t268-snd-b-pending"}
OWNER_A = {
    "t268-own-a-completed",
    "t268-own-a-pending",
    "t268-own-a-cancelled",
    "t268-win-a-pending",
}
OWNERLESS = {"t268-free-ownerless"}
SCOPE_A = ALL_NAMES - SUBMITTED_B  # T-227 scope: own OR sender OR ownerless
WINDOW_MID = {  # created in [2026-11-03T00:00, 2026-11-05T00:00]
    "t268-own-a-pending",
    "t268-free-own-a-failure",
    "t268-own-a-cancelled",
}

_ROWS = [
    ("task_t268_1", "t268-snd-a-running", "running", "NODE_B", "NODE_A", "2026-11-01T08:00:00+00:00"),
    ("task_t268_2", "t268-own-a-completed", "completed", "NODE_A", "NODE_A", "2026-11-02T09:30:00+00:00"),
    ("task_t268_3", "t268-own-a-pending", "pending", "NODE_A", "NODE_A", "2026-11-03T07:15:00+00:00"),
    ("task_t268_4", "t268-free-own-a-failure", "failed", None, "NODE_A", "2026-11-03T23:59:00+00:00"),
    ("task_t268_5", "t268-own-a-cancelled", "cancelled", "NODE_A", "NODE_A", "2026-11-04T12:00:00+00:00"),
    ("task_t268_6", "t268-snd-b-pending", "pending", "NODE_B", "NODE_B", "2026-11-05T16:45:00+00:00"),
    ("task_t268_7", "t268-free-ownerless", "pending", None, None, "2026-11-06T08:00:00+00:00"),
    ("task_t268_8", "t268-win-a-pending", "pending", "NODE_A", "NODE_A", "2026-11-06T18:00:00+00:00"),
]


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test and reset the event-bus history."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t268.db"
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
        auth_limiter.reset()
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


client = TestClient(app)


def _seed_admin() -> str:
    # register-admin accepts ONLY seed_id "master" (core/auth.py:277)
    secret = generate_secret("adm_")
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO admin_seeds (seed_id, seed_hash, role, created_at) VALUES (?, ?, ?, ?)",
            ("master", hash_secret(secret), "admin", "2026-01-01T00:00:00+00:00"),
        )
    )
    conn.commit()
    conn.close()
    return secret


def _register_admin(secret: str) -> tuple:
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "T268 Admin",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["node_id"], body["token"]


def _register_worker(name: str) -> tuple:
    r = client.post(
        "/relay/v2/auth/register",
        json={
            "node_name": name,
            "endpoint": "http://localhost:9001",
            "capabilities": [{"name": "misc.a", "version": "1.0.0"}],
            "role": "service",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["node_id"], body["token"]


def _approve(admin_token: str, worker_id: str) -> str:
    caps = [{"name": "misc.a", "version": "1.0.0"}]
    r = client.post(
        f"/relay/v2/admin/nodes/{worker_id}/approve",
        json={"role": "service", "capabilities": caps},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _seed_world() -> dict:
    """Admin actor + approved non-admin node + bare second node + 8 rows."""
    admin_id, admin_token = _register_admin(_seed_admin())
    node_a, token_a = _register_worker("t268-node-a")
    token_a = _approve(admin_token, node_a)
    node_b = "T268NODEB"
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, status, capabilities,"
            " registered_at, last_seen) VALUES (?, 't268-node-b', 'approved',"
            " '[]', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (node_b,),
        )
    )
    ids = {"NODE_A": node_a, "NODE_B": node_b}

    def _id(value):
        return ids.get(value, value)

    for task_id, name, status_, owner, submitted, created in _ROWS:
        conn.execute(
            q(
                "INSERT INTO tasks (task_id, task_name, status, priority,"
                " owner_node_id, submitted_by, timeout_seconds, created_at,"
                " updated_at) VALUES (?, ?, ?, 0, ?, ?, 300, ?, ?)",
                (task_id, name, status_, _id(owner), _id(submitted), created, created),
            )
        )
    conn.commit()
    conn.close()
    return {
        "admin_id": admin_id,
        "admin_token": admin_token,
        "node_a": node_a,
        "token_a": token_a,
        "node_b": node_b,
    }


def _names(resp) -> set:
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
    return {t["task_name"] for t in resp.json()["tasks"]}


def _list(token, **params) -> set:
    return _names(client.get(BASE + "/tasks", params=params, headers={"Authorization": f"Bearer {token}"}))


# ── admin: per-filter isolation (exact row sets) ─────────────────


def test_admin_submitted_by_filter_isolates_exact_set():
    w = _seed_world()
    assert _list(w["admin_token"], submitted_by=w["node_a"]) == SUBMITTED_A
    assert _list(w["admin_token"], submitted_by=w["node_b"]) == SUBMITTED_B


def test_admin_owner_node_id_filter_isolates_exact_set():
    w = _seed_world()
    assert _list(w["admin_token"], owner_node_id=w["node_a"]) == OWNER_A


def test_admin_created_window_isolates_exact_set():
    w = _seed_world()
    both = _list(
        w["admin_token"],
        created_since="2026-11-03T00:00:00+00:00",
        created_until="2026-11-05T00:00:00+00:00",
    )
    assert both == WINDOW_MID
    assert _list(w["admin_token"], created_since="2026-11-06T00:00:00+00:00") == OWNERLESS | {
        "t268-win-a-pending"
    }
    assert _list(w["admin_token"], created_until="2026-11-02T00:00:00+00:00") == {"t268-snd-a-running"}


def test_admin_filters_combine_and():
    w = _seed_world()
    got = _list(
        w["admin_token"],
        status="pending,completed",
        submitted_by=w["node_a"],
        owner_node_id=w["node_a"],
        created_since="2026-11-03T00:00:00+00:00",
        created_until="2026-11-05T00:00:00+00:00",
    )
    assert got == {"t268-own-a-pending"}


# ── admin: status single vs comma list ────────────────────────────


def test_admin_status_single_value_stays_valid():
    """Back-compat guard: single value must keep working (GREEN pre-change)."""
    w = _seed_world()
    assert _list(w["admin_token"], status="completed") == {"t268-own-a-completed"}
    assert _list(w["admin_token"], status="failed") == {"t268-free-own-a-failure"}


def test_admin_status_comma_list_filters_union():
    w = _seed_world()
    got = _list(w["admin_token"], status="failed,completed")
    assert got == {"t268-own-a-completed", "t268-free-own-a-failure"}
    assert _list(w["admin_token"], status="pending,completed") == {
        "t268-own-a-completed",
        "t268-own-a-pending",
        "t268-snd-b-pending",
        "t268-free-ownerless",
        "t268-win-a-pending",
    }


# ── fail loud: 422 on unknown status / invalid ISO ────────────────


def test_unknown_status_value_422():
    w = _seed_world()
    for bad in ("weird", "completed,bogus", "bogus,completed", ""):
        r = client.get(
            BASE + "/tasks",
            params={"status": bad},
            headers={"Authorization": f"Bearer {w['admin_token']}"},
        )
        assert r.status_code == 422, f"status={bad!r} -> {r.status_code}: {r.text}"


def test_bad_iso_timestamp_422():
    w = _seed_world()
    for param, bad in [("created_since", "not-a-date"), ("created_until", "17"), ("created_since", "2026-13-99")]:
        r = client.get(
            BASE + "/tasks",
            params={param: bad},
            headers={"Authorization": f"Bearer {w['admin_token']}"},
        )
        assert r.status_code == 422, f"{param}={bad!r} -> {r.status_code}: {r.text}"


# ── non-admin guard rails ────────────────────────────────────────


def test_non_admin_foreign_submitted_by_400():
    w = _seed_world()
    r = client.get(
        BASE + "/tasks",
        params={"submitted_by": w["node_b"]},
        headers={"Authorization": f"Bearer {w['token_a']}"},
    )
    assert r.status_code == 400, f"{r.status_code}: {r.text}"


def test_non_admin_foreign_owner_node_id_400():
    w = _seed_world()
    r = client.get(
        BASE + "/tasks",
        params={"owner_node_id": w["node_b"]},
        headers={"Authorization": f"Bearer {w['token_a']}"},
    )
    assert r.status_code == 400, f"{r.status_code}: {r.text}"


def test_non_admin_own_submitted_by_narrows_scope():
    """Sender-scope intersection: own submitted_by filter narrows the visible
    set to rows the node submitted — foreign rows AND ownerless rows that
    carry no sender become invisible."""
    w = _seed_world()
    assert _list(w["token_a"], submitted_by=w["node_a"]) == SUBMITTED_A


def test_non_admin_own_owner_node_id_narrows_scope():
    w = _seed_world()
    assert _list(w["token_a"], owner_node_id=w["node_a"]) == OWNER_A


# ── regression guards (GREEN pre-change by design) ────────────────


def test_non_admin_plain_list_scope_unchanged():
    """T-005g/T-227 fan-out contract: plain non-admin listing still sees
    own-owned OR self-submitted OR ownerless rows — without any filter the
    ownerless sub-tasks MUST stay pollable."""
    w = _seed_world()
    assert _list(w["token_a"]) == SCOPE_A


def test_admin_plain_list_sees_all():
    w = _seed_world()
    assert _list(w["admin_token"]) == ALL_NAMES


# ── core layer: status accepts str OR list ────────────────────────


def test_core_list_tasks_accepts_status_list():
    _seed_world()
    listed = {t["task_name"] for t in Scheduler.list_tasks(status=["pending", "completed"])}
    assert listed == {
        "t268-own-a-completed",
        "t268-own-a-pending",
        "t268-snd-b-pending",
        "t268-free-ownerless",
        "t268-win-a-pending",
    }
    single = {t["task_name"] for t in Scheduler.list_tasks(status="pending")}
    assert single == {
        "t268-own-a-pending",
        "t268-snd-b-pending",
        "t268-free-ownerless",
        "t268-win-a-pending",
    }
