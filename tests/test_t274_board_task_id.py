"""T-274 P8: optional board link via metadata.board_task_id on submit.

Contract (T-274-Row P8): both submit endpoints accept an optional
``metadata.board_task_id`` (project-board row label the submitter wants
linked to the relay task). The relay persists it on the tasks row and
returns it in the TaskView / task detail — the board↔relay link no
longer depends on a name-prefix convention.

Validation is loud (422) for a set-but-invalid board_task_id (wrong
type, empty string, > 255 chars); an absent ``metadata`` keeps the old
shape and stores NULL.
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
from relay_server.main import app

SCHED = "/relay/v2/scheduler"


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t274_board.db"
        settings.heartbeat_interval_seconds = 10
        settings.heartbeat_timeout_multiplier = 5
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
            "INSERT INTO admin_seeds (seed_id, seed_hash, role, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("master", hash_secret(secret), "admin", "2026-01-01T00:00:00+00:00"),
        )
    )
    conn.commit()
    conn.close()
    return secret


def _register_worker(name: str, capabilities: list) -> tuple[str, str]:
    r = client.post(
        "/relay/v2/auth/register",
        json={
            "node_name": name,
            "endpoint": "http://localhost:9001",
            "capabilities": capabilities,
            "role": "service",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["node_id"], body["token"]


def _register_admin(secret: str) -> tuple[str, str]:
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "Admin T274 Board",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    return body["node_id"], body["token"]


def _approve(admin_token: str, worker_id: str, caps: list) -> str:
    """Approve the worker and return its NEW runtime token (the token the
    worker got at registration is superseded by approval — see
    test_owner_node_404.py for the reference flow)."""
    r = client.post(
        f"/relay/v2/admin/nodes/{worker_id}/approve",
        json={"role": "service", "capabilities": caps},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _post_task_simple(worker_token: str, body: dict):
    return client.post(
        f"{SCHED}/task-simple",
        json=body,
        headers={"Authorization": f"Bearer {worker_token}"},
    )


# ── tests ─────────────────────────────────────────────────────────────────


def test_task_simple_persists_board_task_id():
    worker_id, _register_token = _register_worker("board-node", [{"name": "board.cap"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "board.cap"}])

    r = _post_task_simple(worker_token, {
        "capability": "board.cap",
        "payload": {"prompt": "hi"},
        "metadata": {"board_task_id": "T-274"},
    })
    assert r.status_code == 200, r.text
    body = r.json()

    view = client.get(
        f"{SCHED}/tasks/{body['task_id']}",
        headers={"Authorization": f"Bearer {worker_token}"},
    ).json()
    assert view["task"]["board_task_id"] == "T-274"


def test_task_simple_without_metadata_stores_null():
    worker_id, _register_token = _register_worker("board-node", [{"name": "board.cap"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "board.cap"}])

    r = _post_task_simple(worker_token, {
        "capability": "board.cap",
        "payload": {"prompt": "hi"},
    })
    assert r.status_code == 200, r.text
    view = client.get(
        f"{SCHED}/tasks/{r.json()['task_id']}",
        headers={"Authorization": f"Bearer {worker_token}"},
    ).json()
    assert view["task"]["board_task_id"] is None


def test_task_simple_metadata_without_board_task_id_stores_null():
    worker_id, _register_token = _register_worker("board-node", [{"name": "board.cap"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "board.cap"}])

    r = _post_task_simple(worker_token, {
        "capability": "board.cap",
        "payload": {},
        "metadata": {},
    })
    assert r.status_code == 200, r.text
    view = client.get(
        f"{SCHED}/tasks/{r.json()['task_id']}",
        headers={"Authorization": f"Bearer {worker_token}"},
    ).json()
    assert view["task"]["board_task_id"] is None


def test_multistage_tasks_persists_board_task_id():
    """POST /tasks (TaskView response) carries board_task_id directly."""
    worker_id, _register_token = _register_worker("board-node", [{"name": "board.cap"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "board.cap"}])

    r = client.post(
        f"{SCHED}/tasks",
        json={
            "task_name": "board-linked",
            "metadata": {"board_task_id": "T-999"},
            "stages": [
                {"stage_name": "main", "capability": "board.cap", "payload": {"a": 1}},
            ],
        },
        headers={"Authorization": f"Bearer {worker_token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["task"]["board_task_id"] == "T-999"


def test_board_task_id_wrong_type_rejected_422():
    worker_id, _register_token = _register_worker("board-node", [{"name": "board.cap"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "board.cap"}])

    r = _post_task_simple(worker_token, {
        "capability": "board.cap",
        "payload": {},
        "metadata": {"board_task_id": 12345},
    })
    assert r.status_code == 422, r.text


def test_board_task_id_empty_rejected_422():
    worker_id, _register_token = _register_worker("board-node", [{"name": "board.cap"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "board.cap"}])

    r = _post_task_simple(worker_token, {
        "capability": "board.cap",
        "payload": {},
        "metadata": {"board_task_id": ""},
    })
    assert r.status_code == 422, r.text


def test_board_task_id_over_255_chars_rejected_422():
    worker_id, _register_token = _register_worker("board-node", [{"name": "board.cap"}])
    admin_id, admin_token = _register_admin(_seed_admin())
    worker_token = _approve(admin_token, worker_id, [{"name": "board.cap"}])

    r = _post_task_simple(worker_token, {
        "capability": "board.cap",
        "payload": {},
        "metadata": {"board_task_id": "x" * 256},
    })
    assert r.status_code == 422, r.text
