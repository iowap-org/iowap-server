"""T-195: Discovery capability availability (F-07) — acceptance tests (RED state).

Covers the two F-07 bugs in ``core/discovery.get_capabilities``:

1. Order dependency: the first provider creating a ``cap_map`` entry won the
   ``available`` flag, and the one-way correction block could only ever flip
   it to False. With an unavailable provider visited first, the capability
   stayed unavailable even though a usable provider existed — task-simple
   answered 503 while a perfectly good provider was registered.
2. Ghost availability: the per-capability flag (``node_capabilities.available``,
   heartbeat-set, False = provider disabled that capability) was read into
   the per-row cap dict but never used — a disabled capability appeared
   available and accepted tasks that then failed.

SOLL (frozen in design.md): effective provider availability is
``node_available AND capability_available``; the capability aggregate is
``any(effective availability over all providers)``; the ``available_only``
filter works on the effective availability. Order-independent.

Seeding notes (verified live by the architect premise probe):
- The live-nodes query keeps a row when ``last_seen > threshold`` OR
  ``available = FALSE`` — seeds must use real ``now`` timestamps, a fixed
  past timestamp silently drops every available node from the query.
- ``sync_node_capabilities`` opens its own DB connection — the INSERT
  connection must be closed before the sync call (SQLite lock otherwise).
- Distinct ``load`` values pin the ``ORDER BY load ASC`` loop order.

Expected RED state on unmodified main: 3 failed / 1 passed — the
reverse-order test is an order-independence invariant that passes before
AND after the fix. Exact failing lines: design.md §3.2.
"""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.api.v2.auth import limiter as auth_limiter
from relay_server.config import settings
from relay_server.core.auth import generate_secret, hash_secret
from relay_server.core.db import get_conn, init_db, q, sync_node_capabilities
from relay_server.core.discovery import get_capabilities, get_capability_by_name
from relay_server.core.events import event_bus
from relay_server.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test; reset limiter and event bus."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        settings.db_path = db_path
        settings.heartbeat_interval_seconds = 1
        settings.heartbeat_timeout_multiplier = 1
        auth_limiter.reset()
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


client = TestClient(app)


def _seed_admin() -> str:
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


def _register_admin(secret: str) -> tuple[str, str]:
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "Admin T195",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    return body["node_id"], body["token"]


def _seed_node(
    node_id: str,
    name: str,
    available: bool,
    caps: list,
    load: float = 0.0,
) -> None:
    """Directly seed a live node row plus its normalized capability index.

    Real bool binds (T-191 lesson), ``last_seen = now`` (live-query lesson),
    and the index sync runs after the INSERT connection is closed because
    ``sync_node_capabilities`` opens its own connection (auth.py pattern).
    """
    now = datetime.now(UTC).isoformat()
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, capabilities, load, "
            "queue_depth, available, last_seen, registered_at, status, role) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (node_id, name, json.dumps(caps), load, 0, bool(available),
             now, now, "online", "service"),
        )
    )
    conn.commit()
    conn.close()
    sync_node_capabilities(node_id, caps)


def test_capability_order_availability():
    """CAPABILITY_ORDER_AVAILABILITY (F-07 bug 1, forward order).

    The unavailable provider is visited first (lower load). A usable
    provider exists, so the capability must be available — in the
    unfiltered view (task-simple path) AND in the available_only view.
    """
    _seed_node("n-a", "A", available=False, load=0.1,
               caps=[{"name": "cap.order", "available": True}])
    _seed_node("n-b", "B", available=True, load=0.2,
               caps=[{"name": "cap.order", "available": True}])

    # available_only=False — the get_capability_by_name / task-simple path.
    cap = get_capability_by_name("cap.order")
    assert cap is not None
    assert cap["available"] is True
    assert [n["node_id"] for n in cap["nodes"]] == ["n-a", "n-b"]
    assert [n["available"] for n in cap["nodes"]] == [False, True]

    # available_only=True — only the effectively available provider shows.
    avail = [c for c in get_capabilities(available_only=True)
             if c["name"] == "cap.order"]
    assert avail and avail[0]["available"] is True
    assert [n["node_id"] for n in avail[0]["nodes"]] == ["n-b"]


def test_capability_order_availability_reverse():
    """CAPABILITY_ORDER_AVAILABILITY (F-07 bug 1, reverse order — invariant).

    The available provider is visited first. This direction was always
    correct (first-wins True + one-way correction never flips it back);
    it must stay correct after the any()-aggregation rewrite.
    """
    _seed_node("n-b", "B", available=True, load=0.1,
               caps=[{"name": "cap.order", "available": True}])
    _seed_node("n-a", "A", available=False, load=0.2,
               caps=[{"name": "cap.order", "available": True}])

    cap = get_capability_by_name("cap.order")
    assert cap is not None
    assert cap["available"] is True
    assert [n["available"] for n in cap["nodes"]] == [True, False]


def test_unavailable_capability_discovery():
    """UNAVAILABLE_CAPABILITY_DISCOVERY (F-07 bug 2: disabled per-cap flag).

    A live node with one disabled capability (``available: false`` in the
    index): the disabled capability must not appear as available, the
    enabled sibling must stay available, and a second enabled provider
    must flip the aggregate back to True (any() over providers).
    """
    _seed_node("n-c", "C", available=True, load=0.3,
               caps=[{"name": "cap.disabled", "available": False},
                     {"name": "cap.live", "available": True}])

    # available_only=True: the disabled capability is filtered out entirely.
    by_name = {c["name"]: c for c in get_capabilities(available_only=True)}
    assert "cap.disabled" not in by_name
    assert by_name["cap.live"]["available"] is True

    # available_only=False: still visible, but with available=False —
    # on the capability AND on its provider entry (effective availability).
    all_caps = get_capabilities(available_only=False)
    dis = [c for c in all_caps if c["name"] == "cap.disabled"]
    assert dis and dis[0]["available"] is False
    assert dis[0]["nodes"][0]["available"] is False
    live = [c for c in all_caps if c["name"] == "cap.live"]
    assert live and live[0]["available"] is True

    # Second provider enables the capability again: aggregate any() -> True,
    # and the available_only view lists only the effective provider.
    _seed_node("n-d", "D", available=True, load=0.05,
               caps=[{"name": "cap.disabled", "available": True}])
    cap = get_capability_by_name("cap.disabled")
    assert cap is not None
    assert cap["available"] is True
    avail = [c for c in get_capabilities(available_only=True)
             if c["name"] == "cap.disabled"]
    assert avail and avail[0]["available"] is True
    assert [n["node_id"] for n in avail[0]["nodes"]] == ["n-d"]


def test_simple_task_with_available_provider():
    """SIMPLE_TASK_WITH_AVAILABLE_PROVIDER (F-07 e2e: the 503 false negative).

    POST /relay/v2/scheduler/task-simple must create the task (200) when any
    live provider offers the capability — even if an unavailable provider
    was registered first. A capability whose only provider is unavailable
    must still answer 503 (the fix must not disable the gate).
    """
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)

    _seed_node("n-a", "A", available=False, load=0.1,
               caps=[{"name": "cap.e2e", "available": True}])
    _seed_node("n-b", "B", available=True, load=0.2,
               caps=[{"name": "cap.e2e", "available": True}])

    r = client.post(
        "/relay/v2/scheduler/task-simple",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"capability": "cap.e2e", "payload": {"x": 1}},
    )
    assert r.status_code == 200, r.text
    assert r.json().get("task_id")

    # Negative guard: sole provider unavailable -> 503 (unchanged semantics).
    _seed_node("n-dead", "D", available=False, load=0.05,
               caps=[{"name": "cap.dead", "available": True}])
    r2 = client.post(
        "/relay/v2/scheduler/task-simple",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"capability": "cap.dead", "payload": {}},
    )
    assert r2.status_code == 503, r2.text