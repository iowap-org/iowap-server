"""T-206: validate route target origin (T-199 lock).

Starting point: ``upstream`` is a free absolute URL the relay treats
as a proxy target without validation; ``endpoint`` is stored without
validation by the heartbeat. This lets a node abuse the relay as an **open
proxy** against arbitrary targets (loopback, link-local/metadata) — with
``auth: "none"`` even unauthenticated.

This suite pins down the target trust model:

- Blocked target origins (loopback / link-local / multicast / unspecified /
  reserved / IPv4-mapped) are rejected **fail-closed** — without the
  relay even opening a connection.
- RFC1918 stays allowed (the cluster lives there).
- An absolute ``upstream`` is only allowed when the origin matches the
  node's own ``endpoint`` (migration path for legacy routes).
- An invalid ``endpoint`` is stored fail-closed as NULL; the
  node stays online (not: the entire heartbeat 422).
- An invalid route is only discarded, not the entire heartbeat.
"""

import os
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.config import settings
from relay_server.core import auth
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.net_guard import blocked_target_reason
from relay_server.main import app

BASE = "/relay/v2/dashboard"
HEARTBEAT = "/relay/v2/discovery/heartbeat"


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t206.db"
        settings.heartbeat_interval_seconds = 1
        settings.heartbeat_timeout_multiplier = 1
        settings.session_cookie_secure = False
        settings.route_target_allow_hosts = ""
        init_db()
        yield


client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


# ── helpers ──────────────────────────────────────────────────────────────


def _online_node(name: str) -> str:
    node_id, _, _ = auth.register_pending_node(
        name, "http://node.test:9999", [{"name": "t206.cap"}]
    )
    auth.approve_node(node_id)
    conn = get_conn()
    conn.execute(q("UPDATE nodes SET status = 'online' WHERE node_id = ?", (node_id,)))
    conn.commit()
    conn.close()
    return node_id


def _set_endpoint(node_id: str, value: str | None) -> None:
    conn = get_conn()
    conn.execute(q("UPDATE nodes SET endpoint = ? WHERE node_id = ?", (value, node_id)))
    conn.commit()
    conn.close()


def _insert_route(node_id: str, path: str, method: str, auth_mode: str, upstream: str) -> None:
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO node_routes(node_id, path, method, auth, upstream) VALUES (?, ?, ?, ?, ?)",
            (node_id, path, method, auth_mode, upstream),
        )
    )
    conn.commit()
    conn.close()


def _capture_send(monkeypatch) -> list:
    """Record every httpx send; return the list of attempted URLs.

    An emptied list after a refused proxy call proves the relay never even
    opened a connection to the target.
    """
    seen: list = []

    async def fake_send(self, request, stream=True):
        seen.append(str(request.url))
        return httpx.Response(
            200, content=b"ok", headers={"content-type": "text/plain"}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    return seen


def _routes_of(node_id: str) -> list:
    conn = get_conn()
    rows = conn.execute(
        q("SELECT path, upstream FROM node_routes WHERE node_id = ?", (node_id,))
    ).fetchall()
    conn.close()
    return sorted((r["path"], r["upstream"]) for r in rows)


# ── 1. Ziel-Guard (Unit) ─────────────────────────────────────────────────


def test_guard_blocks_loopback_link_local_multicast_and_mapped():
    for host in (
        "127.0.0.1",
        "127.1.2.3",
        "::1",
        "0.0.0.0",
        "169.254.169.254",
        "fe80::1",
        "224.0.0.1",
        "::ffff:127.0.0.1",
    ):
        assert blocked_target_reason(host), f"{host} muss geblockt sein"


def test_guard_allows_cluster_and_public_targets():
    for host in ("192.0.2.60", "10.0.2.3", "172.16.5.5", "93.184.216.34", "storage-node"):
        assert blocked_target_reason(host) is None, f"{host} darf erlaubt sein"


# ── 2. Proxy: blocked and foreign targets ─────────────────────────────────


def test_proxy_refuses_blocked_target_without_connecting(monkeypatch):
    seen = _capture_send(monkeypatch)
    node_id = _online_node("t206-metadata")
    _set_endpoint(node_id, "http://node.test:9999")
    _insert_route(node_id, "/page", "GET", "none", "http://169.254.169.254/latest/meta-data/")

    r = client.get(f"{BASE}/api/node-routes/{node_id}/page")

    assert r.status_code == 502
    assert seen == [], "the relay must not connect to the blocked target"


def test_proxy_refuses_foreign_absolute_upstream(monkeypatch):
    seen = _capture_send(monkeypatch)
    node_id = _online_node("t206-foreign")
    _set_endpoint(node_id, "http://node.test:9999")
    _insert_route(node_id, "/page", "GET", "none", "http://evil.example/page")

    r = client.get(f"{BASE}/api/node-routes/{node_id}/page")

    assert r.status_code == 502
    assert seen == []


def test_proxy_refuses_blocked_upstream_without_endpoint(monkeypatch):
    """Live fleet: 5/6 nodes have endpoint=NULL — still no loopback target."""
    seen = _capture_send(monkeypatch)
    node_id = _online_node("t206-nullendp-blocked")
    _set_endpoint(node_id, None)
    _insert_route(node_id, "/page", "GET", "none", "http://127.0.0.1:8788/internal")

    r = client.get(f"{BASE}/api/node-routes/{node_id}/page")

    assert r.status_code == 502
    assert seen == []


# ── 3. Proxy: migration path and new binding ────────────────────────────


def test_proxy_allows_same_origin_absolute_upstream(monkeypatch):
    """Legacy route (absolute upstream == the node's own origin) keeps running."""
    seen = _capture_send(monkeypatch)
    node_id = _online_node("t206-sameorigin")
    _set_endpoint(node_id, "http://node.test:9999")
    _insert_route(node_id, "/page", "GET", "none", "http://node.test:9999/page?x=1")

    r = client.get(f"{BASE}/api/node-routes/{node_id}/page")

    assert r.status_code == 200
    assert r.text == "ok"
    assert seen == ["http://node.test:9999/page?x=1"]


def test_proxy_rejects_absolute_upstream_without_endpoint(monkeypatch):
    """T-005g (function-test finding): without endpoint, an absolute
    upstream to a foreign origin is now rejected FAIL-CLOSED — also for
    already stored legacy routes (request-time half)."""
    seen = _capture_send(monkeypatch)
    node_id = _online_node("t206-nullendp-absolute")
    _set_endpoint(node_id, None)
    _insert_route(node_id, "/page", "GET", "none", "http://node.test:9999/page")

    r = client.get(f"{BASE}/api/node-routes/{node_id}/page")

    assert r.status_code == 502
    assert seen == [], "fail-closed: no proxy attempt to an unverified origin"


def test_proxy_binds_relative_upstream_to_endpoint(monkeypatch):
    seen = _capture_send(monkeypatch)
    node_id = _online_node("t206-relative")
    _set_endpoint(node_id, "http://node.test:9999")
    _insert_route(node_id, "/page", "GET", "none", "/page?x=1")

    r = client.get(f"{BASE}/api/node-routes/{node_id}/page")

    assert r.status_code == 200
    assert seen == ["http://node.test:9999/page?x=1"]


# ── 4. temp route registration ──────────────────────────────────────────


def test_temp_route_register_rejects_foreign_absolute_upstream():
    node_id, _, _ = auth.register_pending_node(
        "t206-temp-foreign", "http://node.test:9999", [{"name": "t206.temp"}]
    )
    token = auth.approve_node(node_id)
    _set_endpoint(node_id, "http://node.test:9999")

    r = client.post(
        f"{BASE}/api/node-routes/register",
        headers={"Authorization": "Bearer " + str(token)},
        json={
            "path": "/upload/ch1",
            "method": "POST",
            "upstream": "http://evil.example/upload/ch1",
            "channel_id": "ch1",
            "ttl_seconds": 60,
        },
    )

    assert r.status_code == 400
    assert "origin" in r.json()["detail"].lower() or "target" in r.json()["detail"].lower()


def test_temp_route_register_rejects_relative_upstream_without_endpoint():
    node_id, _, _ = auth.register_pending_node(
        "t206-temp-norel", "http://node.test:9999", [{"name": "t206.temp2"}]
    )
    token = auth.approve_node(node_id)
    _set_endpoint(node_id, None)

    r = client.post(
        f"{BASE}/api/node-routes/register",
        headers={"Authorization": "Bearer " + str(token)},
        json={
            "path": "/upload/ch2",
            "method": "POST",
            "upstream": "/upload/ch2",
            "channel_id": "ch2",
            "ttl_seconds": 60,
        },
    )

    assert r.status_code == 400


# ── 5. Heartbeat: Route verwerfen statt Node killen ──────────────────────


def test_heartbeat_drops_foreign_route_but_stays_ok():
    node_id, _, _ = auth.register_pending_node(
        "t206-hb", "http://node.test:9999", [{"name": "t206.hb"}]
    )
    token = auth.approve_node(node_id)

    r = client.post(
        HEARTBEAT,
        headers={"Authorization": "Bearer " + str(token)},
        json={
            "load": 1.0,
            "endpoint": "http://node.test:9999",
            "routes": [
                {"path": "/ok", "method": "GET", "auth": "session", "upstream": "/ok"},
                {
                    "path": "/bad",
                    "method": "GET",
                    "auth": "session",
                    "upstream": "http://evil.example/x",
                },
            ],
        },
    )

    assert r.status_code == 200
    assert _routes_of(node_id) == [("/ok", "/ok")]


def test_heartbeat_stores_blocked_endpoint_as_null():
    """Invalid endpoint -> fail-closed NULL, node stays online."""
    node_id, _, _ = auth.register_pending_node(
        "t206-hb-endp", "http://node.test:9999", [{"name": "t206.hb2"}]
    )
    token = auth.approve_node(node_id)

    r = client.post(
        HEARTBEAT,
        headers={"Authorization": "Bearer " + str(token)},
        json={"load": 1.0, "endpoint": "http://169.254.169.254"},
    )

    assert r.status_code == 200
    conn = get_conn()
    row = conn.execute(q("SELECT endpoint, status FROM nodes WHERE node_id = ?", (node_id,))).fetchone()
    conn.close()
    assert row["endpoint"] is None
    assert row["status"] == "online"
