"""T-193: Dashboard/Auth details (audit findings F-16 / F-17 / F-18).

F-16 (S-01.3): node profile pages and their permanent proxy routes must
only be active while the node is connected. Live statuses (per the
status registry, ``node_live_statuses()``) keep serving; offline and
pending nodes return 404 for the page and the route lookup, so the
proxy no longer forwards to a dead upstream.

F-17 (S-02.5 / S-02.11): ``POST /dashboard/logout`` must exist, honour
the CSRF double-submit contract and expire the session cookies. Today
the change-password page logs out via POST and receives 405 while the
session stays valid.

F-18 (S-02.5): an *online* node must be able to rotate its
registration secret (today ``rotate_registration_secret`` only accepts
``approved``), and recovery via registration secret must return the
rotated secret in the response (today the rotation result is discarded,
the message claims rotation and the *old* secret stays valid).
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
from relay_server.core.session import generate_csrf_token, sign_user_cookie
from relay_server.core.status import node_live_statuses
from relay_server.main import app

BASE = "/relay/v2/dashboard"
REFRESH = "/relay/v2/auth/refresh"


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database for each test (visibility-test pattern).

    ``session_cookie_secure=False`` makes the TestClient cookie jar behave
    like a browser: logout's ``delete_cookie`` (same Path, no Secure
    attribute) replaces a cookie planted without attributes. With the
    production default (Secure) httpx keeps the planted cookie and the
    jar-level session-expiry assertion would be a test artifact.
    """
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t193.db"
        settings.heartbeat_interval_seconds = 1
        settings.heartbeat_timeout_multiplier = 1
        settings.session_cookie_secure = False
        init_db()
        yield


client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


# ── helpers ──────────────────────────────────────────────────────────────


def _admin_session() -> None:
    # httpx stores cookies for single-label hosts under "<host>.local";
    # planting with that domain makes the jar match server Set-Cookies
    # (logout expiry) and request Cookie headers (auth) alike.
    client.cookies.set(
        "relay_user",
        sign_user_cookie({"user_id": "__master__", "username": "master"}),
        domain="testserver.local",
    )


def _csrf_headers() -> dict:
    token = generate_csrf_token()
    client.cookies.set("relay_csrf", token, domain="testserver.local")
    return {"X-CSRF-Token": token}


def _set_status(node_id: str, status_value: str) -> None:
    conn = get_conn()
    conn.execute(q("UPDATE nodes SET status = ? WHERE node_id = ?", (status_value, node_id)))
    conn.commit()
    conn.close()


def _make_node(status_value: str, name: str) -> str:
    """Create a node that ends up in ``status_value`` and return its id."""
    node_id, _, _ = auth.register_pending_node(name, "http://node.test:9999", [{"name": "t193.cap"}])
    if status_value != "pending":
        auth.approve_node(node_id)
    if status_value not in ("approved", "pending"):
        _set_status(node_id, status_value)
    return node_id


def _register_route(node_id: str) -> None:
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO node_routes(node_id, path, method, auth, upstream) "
            "VALUES (?, ?, ?, ?, ?)",
            (node_id, "/page", "GET", "session", "/page"),
        )
    )
    conn.commit()
    conn.close()


# ── F-16: node page liveness gate (S-01.3) ───────────────────────────────


@pytest.mark.parametrize("status_value", node_live_statuses())
def test_live_node_page_serves_profile(status_value):
    """Invariant: every live status keeps serving the profile page."""
    node_id = _make_node(status_value, f"t193-page-{status_value}")
    r = client.get(f"{BASE}/node/{node_id}")
    assert r.status_code == 200
    assert "node-profile.js" in r.text


@pytest.mark.parametrize("status_value", ["offline", "pending"])
def test_non_live_node_page_returns_404(status_value):
    """F-16: offline/pending nodes must not serve an active profile page."""
    node_id = _make_node(status_value, f"t193-page-{status_value}")
    r = client.get(f"{BASE}/node/{node_id}")
    assert r.status_code == 404


# ── F-16: proxy route liveness gate (S-01.3) ─────────────────────────────


def _lookup(node_id: str):
    """Resolve a route via the registry (module imported lazily to avoid
    the route_registry <-> api.v2 circular import at collection time)."""
    from relay_server.core import route_registry

    return route_registry._lookup_route(node_id, "/page", "GET")


@pytest.mark.parametrize("status_value", node_live_statuses())
def test_live_route_lookup_resolves(status_value):
    """Invariant: routes of live nodes keep resolving."""
    node_id = _make_node(status_value, f"t193-route-{status_value}")
    _register_route(node_id)
    assert _lookup(node_id) is not None


@pytest.mark.parametrize("status_value", ["offline", "pending"])
def test_non_live_route_lookup_returns_none(status_value):
    """F-16: routes of offline/pending nodes must not resolve."""
    node_id = _make_node(status_value, f"t193-route-{status_value}")
    _register_route(node_id)
    assert _lookup(node_id) is None


def test_live_proxy_attempts_upstream():
    """Invariant: a live node's route is still proxied (dead upstream -> 502)."""
    _admin_session()
    node_id = _make_node("online", "t193-proxy-live")
    conn = get_conn()
    conn.execute(q("UPDATE nodes SET endpoint = ? WHERE node_id = ?", ("http://127.0.0.1:9", node_id)))
    conn.commit()
    conn.close()
    _register_route(node_id)
    r = client.get(f"{BASE}/api/node-routes/{node_id}/page")
    assert r.status_code == 502


def test_offline_proxy_returns_404():
    """F-16: proxying to an offline node's route must 404, not 502."""
    _admin_session()
    node_id = _make_node("offline", "t193-proxy-offline")
    _register_route(node_id)
    r = client.get(f"{BASE}/api/node-routes/{node_id}/page")
    assert r.status_code == 404


def test_proxy_binds_relative_upstream_to_node_endpoint(monkeypatch):
    """F-21: dynamic routes must use the node endpoint origin, not a stored full URL."""
    _admin_session()
    node_id = _make_node("online", "t193-proxy-bind")
    conn = get_conn()
    conn.execute(
        q("UPDATE nodes SET endpoint = ? WHERE node_id = ?", ("http://node.example:8791/base", node_id))
    )
    conn.execute(
        q(
            "INSERT INTO node_routes(node_id, path, method, auth, upstream) VALUES (?, ?, ?, ?, ?)",
            (node_id, "/page", "GET", "session", "/internal/page?x=1"),
        )
    )
    conn.commit()
    conn.close()

    captured = {}

    async def fake_send(self, request, stream=True):
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            content=b"ok",
            headers={"content-type": "text/plain"},
            request=request,
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)

    r = client.get(f"{BASE}/api/node-routes/{node_id}/page")
    assert r.status_code == 200
    assert r.text == "ok"
    assert captured["url"] == "http://node.example:8791/internal/page?x=1"


def test_temp_route_register_rejects_absolute_upstream():
    """F-21: temp routes must not accept arbitrary absolute upstream URLs."""
    node_id, _, _ = auth.register_pending_node("t193-temp-abs", "http://node.test:9999", [{"name": "t193.cap"}])
    runtime_token = auth.approve_node(node_id)
    assert runtime_token is not None
    _set_status(node_id, "online")
    auth_header = {"Authorization": "Bearer " + runtime_token}
    conn = get_conn()
    conn.execute(q("UPDATE nodes SET endpoint = ? WHERE node_id = ?", ("http://node.test:9999", node_id)))
    conn.commit()
    conn.close()
    r = client.post(
        f"{BASE}/api/node-routes/register",
        headers=auth_header,
        json={
            "path": "/upload/ch1",
            "method": "POST",
            "upstream": "http://evil.example/upload/ch1",
            "channel_id": "ch1",
            "ttl_seconds": 60,
        },
    )
    assert r.status_code == 400
    assert "relative path" in r.json()["detail"]


def test_heartbeat_rejects_absolute_route_upstream():
    """F-21: heartbeat route declarations must reject absolute upstream URLs."""
    node_id, _, _ = auth.register_pending_node("t193-heartbeat-abs", "http://node.test:9999", [{"name": "t193.cap"}])
    runtime_token = auth.approve_node(node_id)
    assert runtime_token is not None
    auth_header = {"Authorization": "Bearer " + runtime_token}
    r = client.post(
        "/relay/v2/discovery/heartbeat",
        headers=auth_header,
        json={
            "endpoint": "http://node.test:9999",
            "routes": [
                {
                    "path": "/page",
                    "method": "GET",
                    "auth": "session",
                    "upstream": "http://evil.example/page",
                }
            ],
        },
    )
    assert r.status_code == 422


# ── F-17: POST /logout contract (S-02.5 / S-02.11) ──────────────────────


def test_logout_post_clears_session_cookies():
    """F-17: POST /logout returns JSON and expires both cookies."""
    _admin_session()
    r = client.post(f"{BASE}/logout", headers=_csrf_headers())
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    set_cookies = r.headers.get_list("set-cookie")
    user_cookies = [c for c in set_cookies if c.startswith("relay_user=")]
    assert user_cookies, "logout must expire the relay_user cookie"
    assert "Max-Age=0" in user_cookies[0]
    csrf_cookies = [c for c in set_cookies if c.startswith("relay_csrf=")]
    assert csrf_cookies, "logout must expire the relay_csrf cookie"
    # End-to-end: the expired session must no longer authenticate.
    assert client.get(f"{BASE}/api/me").status_code == 401


def test_logout_post_requires_csrf():
    """F-17: POST /logout enforces the CSRF double-submit contract."""
    _admin_session()
    r = client.post(f"{BASE}/logout")
    assert r.status_code == 403


def test_logout_get_redirects_to_index():
    """Invariant: GET /logout (navigational) stays a 303 redirect."""
    r = client.get(f"{BASE}/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/relay/v2/dashboard/"


# ── F-18: registration secret rotation + recovery (S-02.5) ───────────────


def test_approved_node_rotation_unaffected():
    """Invariant: approved nodes can still rotate their secret."""
    node_id, _, _ = auth.register_pending_node("t193-rot-approved", None, [{"name": "t193.cap"}])
    rt = auth.approve_node(node_id)
    r = client.post(
        REFRESH,
        headers={"Authorization": f"Bearer {rt}"},
        json={"requested_credential": "registration_secret"},
    )
    assert r.status_code == 200
    assert r.json()["token"].startswith("rs_")


def test_online_node_rotates_registration_secret():
    """F-18: an online node must be able to rotate its secret (was 404)."""
    node_id, _, _ = auth.register_pending_node("t193-rot-online", None, [{"name": "t193.cap"}])
    rt = auth.approve_node(node_id)
    _set_status(node_id, "online")
    r = client.post(
        REFRESH,
        headers={"Authorization": f"Bearer {rt}"},
        json={"requested_credential": "registration_secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("rs_")


def test_rotate_gate_accepts_post_approval_statuses():
    """F-18 unit: every post-approval status may rotate; pending may not."""
    for status_value in ("approved", "online", "idle", "busy", "maintenance", "offline"):
        node_id = _make_node(status_value, f"t193-gate-{status_value}")
        secret = auth.rotate_registration_secret(node_id)
        assert secret is not None, f"rotate must work for status {status_value}"
        assert secret.startswith("rs_")
    pending_id = _make_node("pending", "t193-gate-pending")
    assert auth.rotate_registration_secret(pending_id) is None


def test_recovery_returns_rotated_secret():
    """F-18: recovery must return the rotated secret (was discarded)."""
    node_id, _, rs_old = auth.register_pending_node(
        "t193-recover", None, [{"name": "t193.cap"}]
    )
    auth.approve_node(node_id)
    _set_status(node_id, "offline")
    r = client.post(
        REFRESH,
        json={
            "requested_credential": "runtime_token",
            "node_id": node_id,
            "registration_secret": rs_old,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("rt_")
    rs_new = body.get("registration_secret")
    assert rs_new, "recovery response must carry the rotated registration secret"
    assert rs_new.startswith("rs_")
    assert rs_new != rs_old
    assert body.get("registration_secret_expires_at"), "recovery response must carry the expiry"


def test_old_registration_secret_invalid_after_recovery():
    """F-18: after recovery the old secret must be rejected (was reusable)."""
    node_id, _, rs_old = auth.register_pending_node(
        "t193-reuse", None, [{"name": "t193.cap"}]
    )
    auth.approve_node(node_id)
    _set_status(node_id, "offline")
    first = client.post(
        REFRESH,
        json={
            "requested_credential": "runtime_token",
            "node_id": node_id,
            "registration_secret": rs_old,
        },
    )
    assert first.status_code == 200
    second = client.post(
        REFRESH,
        json={
            "requested_credential": "runtime_token",
            "node_id": node_id,
            "registration_secret": rs_old,
        },
    )
    assert second.status_code == 401