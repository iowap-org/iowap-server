"""Regressionstests für die Funktionstest-Befunde (Card t_6f2b80d5).

B1 — Temp-Route-Registrierung: absoluter Upstream zu fremder Origin muss bei
     endpoint=NULL an der REGISTRIERUNG abgelehnt werden (upstream_reject_reason).
B2 — Task-Read-Scoping: Scheduler.get_task/list_tasks mit owner_node_id
     verbergen fremde Tasks; NULL-owner Tasks bleiben sichtbar (Flow-Contract).
"""
import tempfile
from pathlib import Path

import pytest

from relay_server.config import settings
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.core.net_guard import resolve_route_target, upstream_reject_reason
from relay_server.core.scheduler import Scheduler


@pytest.fixture(autouse=True)
def fresh_db():
    """Fresh temp DB per test (same pattern as test_scheduler_lifecycle)."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "test.db"
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


def _seed_node(node_id: str) -> None:
    """Owner-Node rows must exist (tasks.owner_node_id → nodes FK)."""
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, status, registered_at, last_seen)"
            " VALUES (?, ?, 'online', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (node_id, node_id),
        )
    )
    conn.commit()
    conn.close()


# ── B1: Registrierungs-Guard bei endpoint=None ────────────────

class TestRouteOriginRegistration:
    def test_absolute_upstream_rejected_when_endpoint_none(self):
        # Funktionstest-Repro: evil.example.com wurde bei endpoint=NULL
        # akzeptiert. Jetzt: fail closed.
        reason = upstream_reject_reason("http://evil.example.com/upload", None)
        assert reason is not None
        assert "endpoint" in reason

    def test_absolute_upstream_still_allowed_when_origin_matches(self):
        reason = upstream_reject_reason(
            "http://192.168.2.185:9000/upload", "http://192.168.2.185:9000"
        )
        assert reason is None

    def test_absolute_upstream_other_origin_rejected(self):
        reason = upstream_reject_reason(
            "http://evil.example.com/upload", "http://192.168.2.185:9000"
        )
        assert reason == "absolute upstream must match the node endpoint origin"

    def test_allowlisted_host_still_allowed_without_endpoint(self):
        # Allowlist schlägt Origin-Bind — Loopback selbst ist erst per
        # route_target_allow_hosts erlaubt (Default: blockiert).
        from relay_server.config import settings as cfg

        old = cfg.route_target_allow_hosts
        cfg.route_target_allow_hosts = "127.0.0.1:8788"
        try:
            reason = upstream_reject_reason("http://127.0.0.1:8788/upload", None)
            assert reason is None
        finally:
            cfg.route_target_allow_hosts = old

    def test_relative_upstream_without_endpoint_still_rejected(self):
        assert upstream_reject_reason("/upload/x", None) == "relative upstream requires a node endpoint"

    def test_resolve_target_fails_closed_without_endpoint(self):
        # Request-time-Hälfte: gleiche Lücke dicht, allowlisted bleibt offen.
        url, reason = resolve_route_target("http://evil.example.com/upload", None)
        assert url is None
        assert reason is not None

    def test_resolve_target_allowlisted_without_endpoint_ok(self):
        from relay_server.config import settings as cfg

        old = cfg.route_target_allow_hosts
        cfg.route_target_allow_hosts = "127.0.0.1:8788"
        try:
            url, reason = resolve_route_target("http://127.0.0.1:8788/upload", None)
            assert url == "http://127.0.0.1:8788/upload"
            assert reason is None
        finally:
            cfg.route_target_allow_hosts = old


# ── B2: Owner-Scoping auf Task-Read ───────────────────────────

@pytest.fixture()
def scoped_tasks():
    """Zwei Tasks: einer mit Owner A, einer mit Owner B, einer ohne Owner."""
    _seed_node("NODE_A")
    _seed_node("NODE_B")
    t_a = Scheduler.create_task(task_name="owned-by-a", stages=[{"stage_name": "s", "capability": "x"}], owner_node_id="NODE_A")
    t_b = Scheduler.create_task(task_name="owned-by-b", stages=[{"stage_name": "s", "capability": "x"}], owner_node_id="NODE_B")
    t_free = Scheduler.create_task(task_name="ownerless", stages=[{"stage_name": "s", "capability": "x"}], owner_node_id=None)
    yield {"a": t_a["task_id"], "b": t_b["task_id"], "free": t_free["task_id"]}


class TestTaskOwnerScoping:
    def test_get_task_hides_foreign_owned(self, scoped_tasks):
        # NODE_A sieht Task von NODE_B nicht (B2-Funktionsrepro)
        assert Scheduler.get_task(scoped_tasks["b"], owner_node_id="NODE_A") is None

    def test_get_task_shows_own(self, scoped_tasks):
        assert Scheduler.get_task(scoped_tasks["a"], owner_node_id="NODE_A") is not None

    def test_get_task_shows_ownerless(self, scoped_tasks):
        # Flow-Runner-Contract: NULL-owner Sub-Tasks bleiben pollbar
        assert Scheduler.get_task(scoped_tasks["free"], owner_node_id="NODE_A") is not None

    def test_get_task_unscoped_still_sees_everything(self, scoped_tasks):
        # Admin/Dashboard-Pfad (owner_node_id=None) unverändert
        assert Scheduler.get_task(scoped_tasks["b"]) is not None

    def test_list_scopes_by_owner(self, scoped_tasks):
        names = {t["task_name"] for t in Scheduler.list_tasks(owner_node_id="NODE_A")}
        assert "owned-by-a" in names
        assert "ownerless" in names
        assert "owned-by-b" not in names

    def test_list_scopes_with_status(self, scoped_tasks):
        names = {t["task_name"] for t in Scheduler.list_tasks(status="pending", owner_node_id="NODE_B")}
        assert "owned-by-b" in names
        assert "ownerless" in names
        assert "owned-by-a" not in names

    def test_list_unscoped_sees_all(self, scoped_tasks):
        names = {t["task_name"] for t in Scheduler.list_tasks()}
        assert {"owned-by-a", "owned-by-b", "ownerless"} <= names