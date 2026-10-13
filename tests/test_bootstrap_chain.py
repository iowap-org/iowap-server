"""Bootstrap chaining: creating the first admin logs you in as that admin.

Before this change, ``POST /dashboard/api/bootstrap`` returned
``{user_id, temporary_password}`` with NO session cookies - the operator had
to copy the temp password, log out, and log back in manually. Now the
response carries the new admin's session cookies (``force_password_change``
still set) so bootstrap.js can chain straight into
``POST /dashboard/api/me/password`` with the temp password as a hidden
current_password field - zero to admin in one page flow.
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.config import settings
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.session import generate_csrf_token, sign_user_cookie, unsign_user_cookie
from relay_server.main import app

BASE = "/relay/v2/dashboard"


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t_bootstrap.db"
        settings.session_cookie_secure = False
        init_db()
        yield


client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def _master_session() -> str:
    """Plant a __master__ session + matching CSRF cookie; return the token."""
    client.cookies.set(
        "relay_user",
        sign_user_cookie({"user_id": "__master__", "username": "master"}),
        domain="testserver.local",
    )
    token = generate_csrf_token()
    client.cookies.set("relay_csrf", token, domain="testserver.local")
    return token


def _post_csrf(url: str, data: dict) -> "TestClient.response":
    """POST with the CURRENT csrf cookie value as header (browser behaviour:
    the CSRF cookie rotates with each authenticated response, so read it from
    the jar right before sending)."""
    token = client.cookies.get("relay_csrf", domain="testserver.local")
    return client.post(url, data=data, headers={"X-CSRF-Token": token})


def test_bootstrap_response_logs_in_as_created_admin():
    _master_session()
    r = _post_csrf(
        BASE + "/api/bootstrap", {"username": "ronny", "email": "ronny@example.org"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["temporary_password"]

    # The response REPLACED the master session with the created admin's:
    payload = unsign_user_cookie(client.cookies.get("relay_user"))
    assert payload["user_id"] == body["user_id"]

    # Guard stays: after the cookie swap the session is the new admin, so
    # a second create fails on the master-session guard (the admin-exists
    # guard sits directly behind it for real seed sessions).
    r2 = _post_csrf(BASE + "/api/bootstrap", {"username": "second"})
    assert r2.status_code == 403
    assert r2.json()["detail"] == "Only the master seed session can create the first admin."


def test_chained_password_set_clears_force_flag():
    _master_session()
    r = _post_csrf(BASE + "/api/bootstrap", {"username": "ronny"})
    temp_pw = r.json()["temporary_password"]
    user_id = r.json()["user_id"]

    r2 = _post_csrf(
        BASE + "/api/me/password",
        {"current_password": temp_pw, "new_password": "a-final-password-0123456789"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "ok"

    conn = get_conn()
    try:
        row = conn.execute(
            q("SELECT force_password_change FROM users WHERE user_id = ?", (user_id,))
        ).fetchone()
    finally:
        conn.close()
    assert not row["force_password_change"]