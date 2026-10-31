"""T-258: the node-route proxy strips caller credentials from forwarded headers.

``core/route_registry._forward_headers`` used to strip only the
hop-by-hop headers (host / transfer-encoding / connection). That leaked
two caller credentials to the node's upstream:

* ``Cookie`` — a relay dashboard session cookie is useless to a node
  endpoint (the node API authenticates with node tokens) and its
  forwarding is a pure credential-leak hygiene failure.
* ``Authorization`` — the caller's Bearer token is consumed BY THE RELAY
  (dashboard session or ``node_token`` bridge validation,
  ``get_auth_context``) and never belongs to the upstream call; node
  upstreams authenticate with their OWN node token, which a caller does
  not have. Nothing in the codebase reads a forwarded Bearer token
  upstream-side (``iowap-node``'s file-serve surface carries no auth gate
  that would validate the caller's token), so forwarding it can only leak.

Both are stripped from every forwarded request. This suite pins the
behavior at the proxy boundary (httpx send capture, T-206 pattern).
"""

import os
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.api.v2.auth import limiter as auth_limiter
from relay_server.config import settings
from relay_server.core import auth
from relay_server.core.auth import generate_secret, hash_secret
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.main import app

BASE = "http://testserver/relay/v2/dashboard/api/node-routes"


@pytest.fixture(autouse=True)
def fresh_db():
    """Temporary DB per test; helpers copied from test_forward_routing.py."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t258.db"
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
        settings.session_cookie_secure = False
        settings.route_target_allow_hosts = ""
        auth_limiter.reset()
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


client = TestClient(app, raise_server_exceptions=False)


def _seed_admin() -> str:
    secret = generate_secret("adm_")
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO admin_seeds (seed_id, seed_hash, role, created_at)"
            " VALUES (?, ?, ?, ?)",
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
            "node_name": "Admin T258",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    return body["node_id"], body["token"]


def _register_worker(name: str, caps: list) -> str:
    r = client.post(
        "/relay/v2/auth/register",
        json={
            "node_name": name,
            "endpoint": "http://node.test:9999",
            "capabilities": caps,
            "role": "service",
        },
    )
    assert r.status_code == 200
    return r.json()["node_id"]


def _approve(admin_token: str, worker_id: str, caps: list) -> str:
    r = client.post(
        f"/relay/v2/admin/nodes/{worker_id}/approve",
        json={"role": "service", "capabilities": caps},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    return r.json()["token"]


def _set_online(node_id: str) -> None:
    conn = get_conn()
    conn.execute(q("UPDATE nodes SET status = 'online' WHERE node_id = ?", (node_id,)))
    conn.commit()
    conn.close()


def _set_endpoint(node_id: str, endpoint: str | None) -> None:
    conn = get_conn()
    conn.execute(q("UPDATE nodes SET endpoint = ? WHERE node_id = ?", (endpoint, node_id)))
    conn.commit()
    conn.close()


def _make_online_node(name: str, caps: list | None = None) -> str:
    node_id, _pending, _token = auth.register_pending_node(
        name, "http://node.test:9999", caps or [{"name": "t258.cap"}]
    )
    auth.approve_node(node_id)
    _set_online(node_id)
    return node_id


def _insert_route(node_id: str, path: str, method: str, auth_mode: str, upstream: str):
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO node_routes(node_id, path, method, auth, upstream)"
            " VALUES (?, ?, ?, ?, ?)",
            (node_id, path, method, auth_mode, upstream),
        )
    )
    conn.commit()
    conn.close()


def _capture_send_headers(monkeypatch) -> list:
    """Record the headers of every httpx request the proxy attempts."""
    seen: list = []

    async def fake_send(self, request, stream=True):
        seen.append(dict(request.headers))
        return httpx.Response(
            200, content=b"ok", headers={"content-type": "text/plain"}, request=request
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    return seen


def test_forwarded_headers_drop_cookie_and_authorization(monkeypatch):
    """Browser-style credentials must never reach the node upstream."""
    captured = _capture_send_headers(monkeypatch)
    node_id = _make_online_node("t258-none")
    _insert_route(node_id, "/page", "GET", "none", "http://node.test:9999/page")

    r = client.get(
        f"{BASE}/{node_id}/page",
        cookies={"relay_session": "should-not-travel"},
        headers={"Authorization": "Bearer should-not-travel", "User-Agent": "t258-agent"},
    )

    assert r.status_code == 200
    assert len(captured) == 1
    sent = {k.lower(): v for k, v in captured[0].items()}
    assert "cookie" not in sent, "dashboard session cookie leaked to the node upstream"
    assert "authorization" not in sent, "caller Bearer token leaked to the node upstream"
    assert sent.get("host") is None or sent.get("host") != "testserver", (
        "hop-by-hop host must still be stripped"
    )
    assert sent.get("user-agent") == "t258-agent", "unrelated headers are still forwarded"


def test_node_token_bridge_validates_then_strips_bearer(monkeypatch):
    """A node_token bridge route: the relay validates the caller's Bearer
    token itself; the upstream call carries no Authorization header."""
    captured = _capture_send_headers(monkeypatch)

    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)
    worker_id = _register_worker("t258-bridge", [{"name": "br.cap"}])
    worker_token = _approve(admin_token, worker_id, [{"name": "br.cap"}])
    _set_online(worker_id)
    _set_endpoint(worker_id, "http://node.test:9999")
    _insert_route(
        worker_id, "/bridge", "POST", "node_token", "http://node.test:9999/bridge"
    )

    r = client.post(
        f"{BASE}/{worker_id}/bridge",
        json={"n": 1},
        headers={"Authorization": f"Bearer {worker_token}"},
    )

    assert r.status_code == 200, "the valid bridge Bearer must still authenticate the caller"
    assert len(captured) == 1
    sent = {k.lower(): v for k, v in captured[0].items()}
    assert "authorization" not in sent, "bridge Bearer must not travel to the upstream"
    assert sent.get("content-length") is not None or "content-length" in sent, (
        "T-129: content-length stays forwarded for streamed bodies"
    )


def test_bogus_bridge_bearer_still_rejected_before_proxy(monkeypatch):
    """Stripping authorization must not weaken the node_token bridge gate:
    an invalid Bearer still fails fast, without contacting the upstream."""
    captured = _capture_send_headers(monkeypatch)
    node_id = _make_online_node("t258-bogus")
    _insert_route(node_id, "/b", "GET", "node_token", "http://node.test:9999/b")

    r = client.get(f"{BASE}/{node_id}/b", headers={"Authorization": "Bearer garbage"})

    assert r.status_code == 401
    assert captured == [], "no upstream connection on failed bridge auth"