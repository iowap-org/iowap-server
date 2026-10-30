"""T-256: the ``require_approved``-gate rides the status registry (Audit H2).

``core/auth.py`` ``validate_token(require_approved=True)`` hard-coded
``(approved, online)``: a node that the server itself auto-flipped to
``busy`` or ``idle`` (core/discovery.py auto-busy, queue_depth/load —
the node NEVER requested it) got **401** on ``POST /auth/refresh`` and
``POST /auth/status`` — losing its only credential-lifecycle channel in
the middle of live work.

**Semantics decision (recorded here):** refresh/status-polling is a
LIVENESS channel, not a claim grant — exactly the
``get_alive_context`` registry semantics (api/v2/security.py:71-104):

* ALLOWED: every status in ``node_live_statuses()`` — the AVAILABLE
  category (``approved``, ``online``, ``idle``) plus the BUSY category
  (``busy``, ``maintenance``). ``maintenance`` is node-maintained work
  (BUSY), so it keeps its credential channel.
* FORBIDDEN (fail-closed): ``pending`` (never admin-approved) and
  ``offline`` (UNKNOWN-registry statuses can never pass — the registry
  is the whitelist, not a blocklist).

Hard-codes pulled along (same decision): ``get_approved_context``
(api/v2/security.py:59) and ``require_admin_or_dashboard_user`` (:208) —
both were the same ``(approved, online)`` pair; an auto-busy admin/
worker node must keep submitting tasks (get_approved_context gates the
scheduler submit/list endpoints) and admin API access.

Fixtures follow tests/test_dashboard_auth_details.py (T-193 pattern:
``_make_node``/``_set_status``/``node_live_statuses()`` parametrization).
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.config import settings
from relay_server.core import auth
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.status import node_live_statuses
from relay_server.main import app

REFRESH = "/relay/v2/auth/refresh"
STATUS = "/relay/v2/auth/status"
ADMIN_NODES = "/relay/v2/admin/nodes"


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test; keep the limiter honest."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t256.db"
        settings.heartbeat_interval_seconds = 2
        settings.heartbeat_timeout_multiplier = 3
        init_db()
        yield


client = TestClient(app)


def _set_status(node_id: str, status_value: str) -> None:
    conn = get_conn()
    conn.execute(q("UPDATE nodes SET status = ? WHERE node_id = ?", (status_value, node_id)))
    conn.commit()
    conn.close()


def _make_node(status_value: str, name: str) -> str:
    """Create a node that ends up in ``status_value`` and return its id."""
    node_id, _, _ = auth.register_pending_node(name, None, [{"name": "t256.cap"}])
    if status_value != "pending":
        auth.approve_node(node_id)
    if status_value not in ("approved", "pending"):
        _set_status(node_id, status_value)
    return node_id


def _make_admin_node(status_value: str, name: str) -> tuple[str, str]:
    """Admin-ROLE node ending in ``status_value`` → (node_id, runtime_token).

    register_admin_node CORE path (no HTTP; the HTTP register-admin route
    admits only seed id "master" with a limiter). The node is minted
    ``approved`` — a register-admin node heartbeats into ``online`` in
    real life; here the status is driven DIRECTLY to the auto-busy
    variant (T-080/T-229 pattern: busy/idle are server-set statuses).
    """
    from relay_server.core.auth import register_admin_node

    seed = auth.generate_secret("adm_")
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO admin_seeds (seed_id, seed_hash, role, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("master", auth.hash_secret(seed), "admin", "2026-01-01T00:00:00+00:00"),
        )
    )
    conn.commit()
    conn.close()
    node_id, runtime = register_admin_node(name, seed, None, [{"name": "admin"}])
    if status_value not in ("approved",):
        _set_status(node_id, status_value)
    return node_id, runtime


# ── /auth/status + /auth/refresh (mode 2 — the runtime-token channel) ──


@pytest.mark.parametrize("status_value", node_live_statuses())
def test_status_polling_allowed_for_every_live_status(status_value):
    """Every registry-live status may poll /auth/status (RED: 401 hard-code)."""
    node_id = _make_node(status_value, f"t256-status-{status_value}")
    from relay_server.core.auth import _create_token

    token = _create_token(node_id, f"n-{node_id}", role="service",
                          token_type="runtime", pending=False, ttl_hours=24)
    r = client.post(STATUS, headers={"Authorization": f"Bearer {token}"}, json={})
    assert r.status_code == 200, f"status polling must work while {status_value}"
    assert r.json()["status"] == status_value


@pytest.mark.parametrize("status_value", node_live_statuses())
def test_refresh_allowed_for_every_live_status(status_value):
    """Every registry-live status may refresh its runtime token.

    RED (Audit H2): idle/busy/maintenance got 401 — the server-set
    auto-busy statuses locked the node's credential channel.
    """
    node_id = _make_node(status_value, f"t256-refresh-{status_value}")
    from relay_server.core.auth import _create_token

    token = _create_token(node_id, f"n-{node_id}", role="service",
                          token_type="runtime", pending=False, ttl_hours=24)
    r = client.post(
        REFRESH,
        headers={"Authorization": f"Bearer {token}"},
        json={"requested_credential": "runtime_token"},
    )
    assert r.status_code == 200, f"refresh must work while {status_value}"
    body = r.json()
    assert body["token_type"] == "runtime"
    assert body["token"].startswith("rt_")


@pytest.mark.parametrize("status_value", ["offline", "pending"])
def test_refresh_and_status_forbidden_fail_closed(status_value):
    """Fail-closed contract: pending + offline keep their 401 (registry
    whitelist — a non-registry status can never pass). Pending exercises
    the temporary token; offline exercises the non-live category.
    """
    node_id = _make_node(status_value, f"t256-blocked-{status_value}")
    from relay_server.core.auth import _create_token

    # A runtime-style token minted directly (pending tokens are temporary;
    # both must fail the gate below — never a silent pass).
    token = _create_token(node_id, f"n-{node_id}", role="service",
                          token_type="runtime", pending=(status_value == "pending"),
                          ttl_hours=24)
    for url, body in ((STATUS, {}), (REFRESH, {"requested_credential": "runtime_token"})):
        r = client.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
        assert r.status_code == 401, (
            f"{url} must stay forbidden while {status_value} (fail-closed)"
        )


# ── the two pulled-along hard-codes (security.py:59 / :208) ──────────


@pytest.mark.parametrize("status_value", ["idle", "busy", "maintenance"])
def test_scheduler_submit_gate_accepts_auto_busy_statuses(status_value):
    """get_approved_context (:59) gates the scheduler submit endpoints —
    an auto-busy/idle worker must keep submitting tasks (RED: 403 today).
    """
    _node_id, token = _make_admin_node(status_value, f"t256-ctx-{status_value}")
    r = client.post(
        "/relay/v2/scheduler/tasks",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "task_name": "t256",
            "stages": [{"stage_name": "s1", "capability": "t256.cap"}],
        },
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("status_value", ["idle", "busy", "maintenance"])
def test_admin_api_gate_accepts_auto_busy_statuses(status_value):
    """require_admin_or_dashboard_user (:208): admin ROLE node not in
    (approved, online) keeps admin API access (RED: 403 'Node not
    approved' today)."""
    _node_id, token = _make_admin_node(status_value, f"t256-admin-{status_value}")
    r = client.get(ADMIN_NODES, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("status_value", ["offline", "pending"])
def test_admin_api_gate_fail_closed(status_value):
    """Fail-closed: offline/pending admin nodes stay out (:208 path)."""
    if status_value == "pending":
        _node_id, token, _rs = auth.register_pending_node(
            "t256-admin-pending", None, [{"name": "admin"}]
        )
    else:
        _node_id, token = _make_admin_node(status_value, "t256-admin-offline")
    r = client.get(ADMIN_NODES, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (401, 403), (
        f"admin API must stay forbidden while {status_value}"
    )