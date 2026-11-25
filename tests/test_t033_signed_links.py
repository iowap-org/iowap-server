"""T-033: signed artifact download links — acceptance tests.

Covers the brief's 10 requirements:

1.  Link-create happy path → URL shape + expires_at around the default TTL.
2.  TTL clamps: 10 s and 100 000 s → 422 (out-of-range is rejected, never
    silently clamped).
3.  max_downloads clamp: 99 → 422 (consistent with the TTL variant: the
    server REJECTS out-of-range instead of soft-capping to 10).
4.  Anonymous download with a valid token → 200 + exact file bytes.
5.  Second request at max=1 → 404 "link exhausted"; ``downloads`` counted.
6.  Manipulated token (one char different) → 404 via hash lookup.
7.  Expired link → 404 "link expired".
8.  Link-CREATE with a dead token (no real auth) → 401/403 depending on
    the context gate.
9.  ``created_by`` records "admin" for an admin token and the node_id for
    a worker token.
10. Purge task: a link expired > 1 day ago is deleted, a fresh one stays.

The link GET is unauthenticated by design (the URL is the bearer); every
failure mode answers 404 — never 401/403 (no token-probing oracle).
"""

import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay_server.api.v2.auth import limiter as auth_limiter
from relay_server.config import settings
from relay_server.core.artifacts import (
    store_artifact,
    purge_expired_artifact_links,
)
from relay_server.core.auth import _create_token, generate_secret, hash_secret
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    """Temporary database + artifacts dir per test (suite convention)."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "test.db"
        settings.artifacts_dir = Path(tmp) / "artifacts"
        auth_limiter.reset()
        event_bus.clear()
        init_db()
        yield Path(tmp)
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


# 2 s clock slack for expires_at boundary checks below.
_EXPIRY_SLACK = 2.0


def _register_admin(secret: str) -> str:
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "T033 admin",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _seed_worker_and_token(node_id: str = "nwk033") -> str:
    """Mint a live worker node token for the non-admin created_by branch."""
    now = datetime.now(UTC).isoformat()
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, capabilities, load, queue_depth,"
            " available, last_seen, registered_at, status, role)"
            " VALUES (?, ?, '[]', 0.0, 0, 1, ?, ?, 'online', 'worker')",
            (node_id, node_id, now, now),
        )
    )
    conn.commit()
    conn.close()
    return _create_token(
        node_id,
        node_id,
        role="worker",
        token_type="runtime",
        pending=False,
        ttl_hours=1,
    )


# 2 s clock slack for expires_at boundary checks below.
_EXPIRY_SLACK = 2.0


def _link(artifact_id: str, token: str, *, headers=None, body=None):
    """Create a signed link and return the raw Response for assertions."""
    if body is None:
        body = {}
    if headers is None:
        headers = {"Authorization": f"Bearer {token}"}
    r = client.post(
        f"/relay/v2/storage/files/{artifact_id}/link",
        json=body,
        headers=headers,
    )
    return r


def _artifact(content: bytes = b"t033-bytes", name: str = "t033.bin"):
    return store_artifact(name=name, content=content, created_by=None)


def _row_for_artifact(artifact_id: str):
    conn = get_conn()
    try:
        return conn.execute(
            q("SELECT * FROM artifact_links WHERE artifact_id = ?", (artifact_id,))
        ).fetchone()
    finally:
        conn.close()


# ── 1. happy path ────────────────────────────────────────────────


def test_1_link_create_happy_path_url_shape_and_expires_at():
    token = _register_admin(_seed_admin())
    art = _artifact()
    before = time.time()
    r = _link(art["artifact_id"], token)
    assert r.status_code == 200, r.text
    body = r.json()
    after = time.time()
    assert body["artifact_id"] == art["artifact_id"]
    # URL shape: base + the real mounted path prefix + token fragment.
    assert "/dl/" in body["url"]
    assert f"/storage/files/{art['artifact_id']}/dl/" in body["url"]
    assert body["url"].startswith("http")
    assert body["max_downloads"] == 1
    assert body["ttl_seconds"] == 900
    assert body["max_downloads_cap"] == 10
    # expires_at = creation time + 900 s (default TTL); the minting happens
    # somewhere in [before, after], so the result must sit inside
    # [before + 900 - slack, after + 900 + slack].
    expires = datetime.fromisoformat(body["expires_at"]).timestamp()
    assert before + 900 - _EXPIRY_SLACK <= expires <= after + 900 + _EXPIRY_SLACK, (before, after, expires)


# ── 2. TTL clamps ────────────────────────────────────────────────


def test_2_ttl_clamps_below_and_above_range_rejected_422():
    token = _register_admin(_seed_admin())
    art = _artifact()
    for bad_ttl in (10, 100_000):
        r = _link(art["artifact_id"], token, body={"ttl_seconds": bad_ttl})
        assert r.status_code == 422, (bad_ttl, r.text)
        assert "ttl_seconds" in r.json()["detail"]
    # The accepted bounds themselves pass (sanity: min 30, max 86400).
    assert _link(art["artifact_id"], token, body={"ttl_seconds": 30}).status_code == 200
    assert _link(art["artifact_id"], token, body={"ttl_seconds": 86_400}).status_code == 200


# ── 3. max_downloads clamp ───────────────────────────────────────


def test_3_max_downloads_99_rejected_422_consistent_with_ttl():
    """Cohort choice (brief allows capped-10 OR 422): the server REJECTS
    out-of-range max_downloads with 422 — consistent with the TTL clamp
    variant, which also answers 422 instead of silently softening."""
    token = _register_admin(_seed_admin())
    art = _artifact()
    r = _link(art["artifact_id"], token, body={"max_downloads": 99})
    assert r.status_code == 422, r.text
    assert "max_downloads" in r.json()["detail"]
    # The cap boundary itself (10) is accepted.
    ok = _link(art["artifact_id"], token, body={"max_downloads": 10})
    assert ok.status_code == 200, ok.text
    assert ok.json()["max_downloads"] == 10


# ── 4. anonymous download ────────────────────────────────────────


def test_4_anonymous_download_with_valid_token_returns_bytes():
    token = _register_admin(_seed_admin())
    art = _artifact(content=b"t033-anon-bytes")
    body = _link(art["artifact_id"], token).json()
    r = client.get(body["url"])  # NO Authorization header on purpose
    assert r.status_code == 200, r.text
    assert r.content == b"t033-anon-bytes"


# ── 5. exhaustion + counter ──────────────────────────────────────


def test_5_second_download_exhausted_404_and_downloads_counted():
    token = _register_admin(_seed_admin())
    art = _artifact()
    body = _link(art["artifact_id"], token).json()
    first = client.get(body["url"])
    assert first.status_code == 200, first.text
    row = _row_for_artifact(art["artifact_id"])
    assert row["downloads"] == 1
    second = client.get(body["url"])
    assert second.status_code == 404, second.text
    assert second.json()["detail"] == "link exhausted"
    # Counter untouched by the reject (the UPDATE guard never fired).
    assert _row_for_artifact(art["artifact_id"])["downloads"] == 1


# ── 6. manipulated token ─────────────────────────────────────────


def test_6_manipulated_token_404():
    token = _register_admin(_seed_admin())
    art = _artifact()
    body = _link(art["artifact_id"], token).json()
    url = body["url"]
    good = url.rsplit("/", 1)[-1]
    tampered = ("A" if good[0] != "A" else "B") + good[1:]
    r = client.get(url.rsplit("/", 1)[0] + "/" + tampered)
    assert r.status_code == 404, r.text
    # The genuine token still works — the row was never touched.
    assert client.get(url).status_code == 200


# ── 7. expired link ──────────────────────────────────────────────


def test_7_expired_link_404():
    token = _register_admin(_seed_admin())
    art = _artifact()
    body = _link(art["artifact_id"], token).json()
    conn = get_conn()
    conn.execute(
        q("UPDATE artifact_links SET expires_at = ? WHERE artifact_id = ?",
          (time.time() - 10.0, art["artifact_id"])),
    )
    conn.commit()
    conn.close()
    r = client.get(body["url"])
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "link expired"


# ── 8. dead token at create ──────────────────────────────────────


def test_8_create_with_dead_token_rejected_401_or_403():
    _seed_admin()  # suite shape parity; the dead token itself is bogus
    art = _artifact()
    r = _link(art["artifact_id"], "not-a-real-token")
    assert r.status_code in (401, 403), (r.status_code, r.text)
    # No link row must have been created by the rejected call.
    assert _row_for_artifact(art["artifact_id"]) is None


# ── 9. created_by audit trail ────────────────────────────────────


def test_9_created_by_admin_flag_and_node_id():
    admin_token = _register_admin(_seed_admin())
    art_admin = _artifact(name="t033-admin.bin")
    _link(art_admin["artifact_id"], admin_token)
    row = _row_for_artifact(art_admin["artifact_id"])
    assert row["created_by"] == "admin"

    worker_token = _seed_worker_and_token("nwk033x")
    art_worker = _artifact(name="t033-worker.bin")
    _link(art_worker["artifact_id"], worker_token)
    row = _row_for_artifact(art_worker["artifact_id"])
    assert row["created_by"] == "nwk033x"


# ── 10. purge ────────────────────────────────────────────────────


def test_10_purge_reaps_expired_older_than_1_day_keeps_fresh():
    epoch_now = time.time()
    conn = get_conn()
    stale_expired = epoch_now - (86_400.0 + 60.0)  # expired > 1 day ago
    fresh_expired = epoch_now - 60.0               # expired, but < 1 day ago
    still_valid = epoch_now + 900.0
    for link_id, expires, token_hash in (
        ("lnk_old0000000000001", stale_expired, "a" * 64),
        ("lnk_justexpired00001", fresh_expired, "b" * 64),
        ("lnk_fresh00000000001", still_valid, "c" * 64),
    ):
        conn.execute(
            q("""
            INSERT INTO artifact_links
            (link_id, artifact_id, token_hash, created_by, max_downloads, downloads, expires_at, created_at)
            VALUES (?, 'artifact_fresh', ?, 'admin', 1, 0, ?, ?)
            """, (link_id, token_hash, expires, epoch_now - 100)),
        )
    conn.commit()
    conn.close()

    result = purge_expired_artifact_links(older_than_days=1.0)

    assert result["deleted"] == 1
    assert result["freed_bytes"] == 0 and result["failed"] == 0 and result["failed_bytes"] == 0
    conn = get_conn()
    try:
        remaining = {r[0] for r in conn.execute(q("SELECT link_id FROM artifact_links")).fetchall()}
    finally:
        conn.close()
    # Stale (> 1 day past expiry) reaped; fresh + just-expired stay for audit.
    assert remaining == {"lnk_justexpired00001", "lnk_fresh00000000001"}
    # Idempotent re-run: nothing left in the window to reap.
    assert purge_expired_artifact_links(older_than_days=1.0)["deleted"] == 0
