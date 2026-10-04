"""T-215: Dashboard summary must report total_capabilities.

The admin overview card ("capabilities": ``admin.js:215``) reads
``summary.total_capabilities`` with ``?? 0``, while
``dashboard_overview`` never set that key — the counter showed a
permanent 0 even with caps advertised and nodes online.

Contract: ``total_capabilities`` counts DISTINCT capability names across
all non-admin nodes regardless of online status (matches the
discovery registry, which also lists offline nodes).
"""

import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

os_guard = __import__("os")
os_guard.environ["RELAY_DB_PATH"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from relay_server.config import settings  # noqa: E402
from relay_server.core import auth  # noqa: E402
from relay_server.core.db import get_conn, init_db, q  # noqa: E402
from relay_server.core.session import sign_user_cookie  # noqa: E402
from relay_server.main import app  # noqa: E402

BASE = "/relay/v2/dashboard"

client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def fresh_db():
    """Temporary database per test (visibility-test pattern)."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t215.db"
        init_db()
        yield


def _admin_session() -> None:
    client.cookies.set(
        "relay_user",
        sign_user_cookie({"user_id": "__master__", "username": "master"}),
        domain="testserver.local",
    )


def _make_node(name: str, caps: list[dict], status_value: str = "online") -> str:
    """Register + approve a node, then force its status; return node_id."""
    node_id, _, _ = auth.register_pending_node(name, None, caps)
    if status_value != "pending":
        auth.approve_node(node_id)
    if status_value not in ("approved", "pending"):
        conn = get_conn()
        conn.execute(q("UPDATE nodes SET status = ? WHERE node_id = ?", (status_value, node_id)))
        conn.commit()
        conn.close()
    return node_id


def _overview_summary() -> dict:
    r = client.get(f"{BASE}/api/overview")
    assert r.status_code == 200
    return r.json()["summary"]


def _cap_api() -> list:
    r = client.get(f"{BASE}/api/capabilities")
    assert r.status_code == 200
    return r.json()["capabilities"]


def test_summary_total_capabilities_counts_distinct_names() -> None:
    """Two nodes, overlapping caps: unique names counted once."""
    _admin_session()
    _make_node(
        "t215-a",
        [{"name": "video.gen.ltx"}, {"name": "image.gen.mflux"}],
    )
    _make_node(
        "t215-b",
        [{"name": "image.gen.mflux"}, {"name": "chat.basic"}],
    )

    summary = _overview_summary()
    assert summary["total_capabilities"] == 3  # ltx, mflux, chat

    # Cross-check: the capabilities-tab endpoint must list exactly those.
    cap_names = {c["name"] for c in _cap_api()}
    assert cap_names == {"video.gen.ltx", "image.gen.mflux", "chat.basic"}


def test_summary_total_capabilities_includes_offline_nodes() -> None:
    """Offline nodes still advertise caps (same contract as discovery)."""
    _admin_session()
    _make_node("t215-off", [{"name": "vault"}], status_value="offline")

    summary = _overview_summary()
    assert summary["total_capabilities"] == 1


def test_summary_total_capabilities_empty_cluster_is_zero() -> None:
    """Fresh cluster → key present, 0, not null."""
    _admin_session()
    summary = _overview_summary()
    assert summary["total_capabilities"] == 0


def test_summary_total_capabilities_dedupe_within_one_node() -> None:
    """Same name twice on one node → counted once (DISTINCT contract)."""
    _admin_session()
    _make_node(
        "t215-dup",
        [{"name": "dup.cap", "version": "1.0"}, {"name": "dup.cap", "version": "2.0"}],
    )
    summary = _overview_summary()
    assert summary["total_capabilities"] == 1