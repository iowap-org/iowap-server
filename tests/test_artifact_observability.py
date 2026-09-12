"""T-192: Artifact-store, node-delete claims, and observability — acceptance tests.

Covers five audit findings (RED against unmodified main):

* F-19 — admin node delete must fail the deleted node's claimed stages
  and its tasks (no ownerless ``claimed`` rows with NULL claim fields).
* F-13 — transfer-config ladder values are BYTES; raw MiB slider values
  are rejected by the server (documented invariant, green before+after;
  the actual fix is client-side in static/admin.js).
* F-14 — TTL cleanup keeps the DB row when the file unlink fails and
  reports ``failed`` / ``failed_bytes`` counters instead of counting the
  bytes as freed.
* F-15a — re-uploading the same chunk index replaces the on-disk slot;
  ``total_bytes`` must reflect the current slots (replacement difference),
  not cumulative upload bytes.
* F-15b — ``prune_stale`` also reaps orphan session directories on disk
  that are unknown to the in-memory session map (restart case).
* F-20 — a maintenance sweep with failing tasks degrades ``/ready`` via
  ``maintenance_last_ok``; Prometheus export gains a ``+Inf`` histogram
  bucket and escapes counter label values.

Fixture pattern follows tests/test_discovery_visibility.py (fresh_db
autouse fixture, TestClient, ``RELAY_DB_PATH`` env reset). Node rows are
seeded via direct DB insert (T-189 lesson: ``claimed_by`` has a foreign
key to ``nodes`` — the node row must exist before claiming a stage).
"""

import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.api.v2.auth import limiter as auth_limiter
from relay_server.config import settings
from relay_server.core import metrics as metrics_mod
from relay_server.core.artifacts import cleanup_expired_artifacts
from relay_server.core.auth import generate_secret, hash_secret
from relay_server.core.chunked_upload import ChunkedUploadManager
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.core.session import sign_user_cookie
from relay_server.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database per test and reset the event-bus history."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        settings.db_path = db_path
        settings.heartbeat_interval_seconds = 1
        settings.heartbeat_timeout_multiplier = 1
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


def _register_admin(secret: str) -> tuple[str, str]:
    r = client.post(
        "/relay/v2/auth/register-admin",
        json={
            "node_name": "Admin T192",
            "bootstrap_secret": secret,
            "capabilities": [{"name": "admin", "version": "1.0.0"}],
        },
    )
    assert r.status_code == 200
    body = r.json()
    return body["node_id"], body["token"]


def _seed_node_row(node_id: str) -> None:
    """Insert a node row directly (claimed_by has an FK to nodes)."""
    now = datetime.now(UTC).isoformat()
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, capabilities, load, "
            "queue_depth, available, last_seen, registered_at, status, role) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (node_id, "Node X", "[]", 0.0, 0, 1, now, now, "online", "worker"),
        )
    )
    conn.commit()
    conn.close()


def _seed_task_with_two_stages(task_id: str, stage_a: str, stage_b: str) -> None:
    """Seed a running task + 2 pending stages directly in the DB."""
    now = datetime.now(UTC).isoformat()
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO tasks (task_id, task_name, status, priority, "
            "timeout_seconds, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, "T192 task", "running", 0, 300, now, now),
        )
    )
    for stage_id, seq in ((stage_a, 0), (stage_b, 1)):
        conn.execute(
            q(
                "INSERT INTO task_stages (stage_id, task_id, stage_name, "
                "capability, depends_on, status, sequence, timeout_seconds, "
                "payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (stage_id, task_id, f"stage-{seq}", "probe.cap", "[]", "pending",
                 seq, 300, "{}", now, now),
            ),
        )
    conn.commit()
    conn.close()


def _stage_row(stage_id: str):
    conn = get_conn()
    row = conn.execute(
        q("SELECT * FROM task_stages WHERE stage_id = ?", (stage_id,))
    ).fetchone()
    conn.close()
    return row


def _stage_status(stage_id: str) -> str | None:
    row = _stage_row(stage_id)
    return row["status"] if row else None


def _task_status(task_id: str) -> str | None:
    conn = get_conn()
    row = conn.execute(
        q("SELECT status FROM tasks WHERE task_id = ?", (task_id,))
    ).fetchone()
    conn.close()
    return row["status"] if row else None


def _events_of_type(etype: str) -> list[dict]:
    return [e for e in event_bus.recent(200) if e.get("type") == etype]


# ── F-19: node delete fails claimed stages ─────────────────────────────


def test_node_delete_fails_claimed_stages():
    """F-19: deleting a node fails its claimed stages + the task (no
    ownerless NULL-claim rows). Stage B stays pending (no cascade)."""
    secret = _seed_admin()
    _admin_id, admin_token = _register_admin(secret)

    _seed_task_with_two_stages("task_f19", "stage_f19_a", "stage_f19_b")
    _seed_node_row("n-f19")
    now = datetime.now(UTC)
    conn = get_conn()
    conn.execute(
        q(
            "UPDATE task_stages SET status = 'claimed', claimed_by = ?, "
            "claimed_at = ?, claim_expires_at = ? WHERE stage_id = ?",
            ("n-f19", now.isoformat(),
             (now + timedelta(hours=1)).isoformat(), "stage_f19_a"),
        )
    )
    conn.commit()
    conn.close()

    r = client.delete(
        "/relay/v2/admin/nodes/n-f19",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.text

    # Claimed stage is failed, not left ownerless-claimed.
    assert _stage_status("stage_f19_a") == "failed", (
        "F-19: claimed stage must be failed on node delete, "
        f"got {_stage_status('stage_f19_a')!r}"
    )
    # Claim fields are cleared on the failed row (invariant: also true on main).
    assert _stage_row("stage_f19_a")["claimed_by"] is None
    # Linear failure policy: the task fails too.
    assert _task_status("task_f19") == "failed", (
        "F-19: task with a node-deleted stage must fail "
        f"(got {_task_status('task_f19')!r})"
    )
    # Downstream stage stays pending (no cascade cancel — T-189 decision).
    assert _stage_status("stage_f19_b") == "pending"

    # Events: stage_failed carries reason='node_deleted'.
    stage_failed = _events_of_type("stage_failed")
    assert any(
        e.get("payload", {}).get("stage_id") == "stage_f19_a"
        and e.get("payload", {}).get("reason") == "node_deleted"
        for e in stage_failed
    ), f"stage_failed(node_deleted) event missing: {stage_failed}"
    # status_changed published for the stage and the task.
    assert any(
        e.get("payload", {}).get("entity_type") == "stage"
        and e.get("payload", {}).get("entity_id") == "stage_f19_a"
        and e.get("payload", {}).get("new_status") == "failed"
        for e in _events_of_type("status_changed")
    ), "stage status_changed event missing"
    assert any(
        e.get("payload", {}).get("entity_type") == "task"
        and e.get("payload", {}).get("entity_id") == "task_f19"
        and e.get("payload", {}).get("new_status") == "failed"
        for e in _events_of_type("status_changed")
    ), "task status_changed event missing"


# ── F-13: transfer-config ladder units (server invariant) ───────────────


def test_transfer_form_units_roundtrip():
    """F-13 (server invariant, green before+after): the ladder endpoint
    accepts byte values and rejects raw MiB values with 400. The actual
    unit bug is client-side (static/admin.js) and verified in the coder
    smoke via a string assertion on the conversion."""
    cookie = sign_user_cookie({"user_id": "__master__", "username": "master"})
    headers = {
        "Cookie": f"relay_user={cookie}; relay_csrf=probe-csrf",
        "x-csrf-token": "probe-csrf",
    }
    saved = (
        settings.max_inline_bytes,
        settings.max_artifact_bytes,
        settings.artifact_ttl_days,
    )
    try:
        # Byte values are accepted and round-trip via transfer-status.
        r = client.post(
            "/relay/v2/dashboard/api/transfer-config",
            headers=headers,
            data={
                "max_inline_bytes": str(5 * 1024 * 1024),
                "max_artifact_bytes": str(50 * 1024 * 1024),
                "artifact_ttl_days": "7",
            },
        )
        assert r.status_code == 200, r.text
        body = client.get(
            "/relay/v2/dashboard/api/transfer-status", headers=headers
        ).json()
        assert body["max_inline_bytes"] == 5 * 1024 * 1024
        assert body["max_artifact_bytes"] == 50 * 1024 * 1024
        assert body["artifact_ttl_days"] == 7

        # Raw slider MiB values (5 / 50) are rejected — the server wants
        # bytes; sending MiB units is the F-13 bug the JS fix removes.
        r3 = client.post(
            "/relay/v2/dashboard/api/transfer-config",
            headers=headers,
            data={
                "max_inline_bytes": "5",
                "max_artifact_bytes": "50",
                "artifact_ttl_days": "7",
            },
        )
        assert r3.status_code == 400, (
            f"raw MiB values must be rejected, got {r3.status_code}: {r3.text}"
        )
    finally:
        (
            settings.max_inline_bytes,
            settings.max_artifact_bytes,
            settings.artifact_ttl_days,
        ) = saved


# ── F-14: TTL cleanup survives unlink failures ─────────────────────────


def test_ttl_cleanup_survives_unlink_failure(tmp_path):
    """F-14: when unlink fails the DB row stays, bytes are not counted
    as freed, and the result reports ``failed`` / ``failed_bytes``."""
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    art_file = art_dir / "probe.bin"
    art_file.write_bytes(b"x" * 1234)

    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO artifacts (artifact_id, name, storage_path, size_bytes, "
            "created_at) VALUES (?, ?, ?, ?, ?)",
            ("art_f14", "probe.bin", str(art_file), 1234,
             "2020-01-01T00:00:00+00:00"),
        )
    )
    conn.commit()
    conn.close()

    # Read-only parent dir -> unlink raises PermissionError on Linux.
    art_dir.chmod(0o555)
    try:
        result = cleanup_expired_artifacts(max_age_days=0)
        c = get_conn()
        row = c.execute(
            q("SELECT artifact_id FROM artifacts WHERE artifact_id = ?",
              ("art_f14",)),
        ).fetchone()
        c.close()
    finally:
        art_dir.chmod(0o755)

    assert result["failed"] == 1, f"expected failed=1, got {result}"
    assert result["failed_bytes"] == 1234, f"expected failed_bytes=1234, got {result}"
    assert result["deleted"] == 0
    assert result["freed_bytes"] == 0
    assert row is not None, "F-14: DB row must survive a failed unlink"
    assert art_file.exists(), "F-14: file must survive a failed unlink"

    # Repair path: with the directory writable again the next sweep
    # deletes row + file and counts the freed bytes.
    result2 = cleanup_expired_artifacts(max_age_days=0)
    c = get_conn()
    row2 = c.execute(
        q("SELECT artifact_id FROM artifacts WHERE artifact_id = ?",
          ("art_f14",)),
    ).fetchone()
    c.close()
    assert result2["deleted"] == 1, f"repair sweep failed: {result2}"
    assert result2["freed_bytes"] == 1234
    assert result2["failed"] == 0
    assert row2 is None
    assert not art_file.exists()


# ── F-15a: chunk replacement accounting ────────────────────────────────


def test_chunk_retry_accounting(tmp_path):
    """F-15a: re-sending chunk 0 replaces the slot; total_bytes reflects
    the current slots (8), and a correct total is not rejected by the
    upload-size limit that the over-counted total would exceed."""
    mgr = ChunkedUploadManager(base_dir=tmp_path / "chunks")
    uid = mgr.init_upload(
        name="probe.bin", mime_type="application/octet-stream", total_chunks=2
    )["upload_id"]
    mgr.store_chunk(uid, 0, b"AAAA")       # slot 0: 4 bytes
    mgr.store_chunk(uid, 0, b"BBBBBB")     # slot 0 replaced: 6 bytes
    mgr.store_chunk(uid, 1, b"CC")         # slot 1: 2 bytes
    session = mgr.get_session(uid)
    assert session["total_bytes"] == 8, (
        f"F-15a: total_bytes must be 8 (4→6 replacement + 2), "
        f"got {session['total_bytes']}"
    )

    # Limit scenario: over-counted total (12) would exceed a 10-byte cap,
    # the correct total (8) must not be rejected.
    old_limit = settings.max_upload_bytes
    settings.max_upload_bytes = 10
    try:
        mgr2 = ChunkedUploadManager(base_dir=tmp_path / "chunks2")
        uid2 = mgr2.init_upload(
            name="probe2.bin", mime_type=None, total_chunks=2
        )["upload_id"]
        mgr2.store_chunk(uid2, 0, b"AAAA")
        mgr2.store_chunk(uid2, 0, b"BBBBBB")
        mgr2.store_chunk(uid2, 1, b"CC")
        assert mgr2.get_session(uid2)["total_bytes"] == 8
    finally:
        settings.max_upload_bytes = old_limit


# ── F-15b: prune_stale reaps orphan session dirs ───────────────────────


def test_prune_stale_orphan_dirs(tmp_path):
    """F-15b: session dirs on disk that are unknown to the in-memory
    session map (restart case) are pruned by directory mtime age; fresh
    dirs stay."""
    base = tmp_path / "uploads"
    base.mkdir()
    orphan = base / "upl_orphan123"
    orphan.mkdir()
    (orphan / "chunk_0000").write_bytes(b"x" * 10)
    old = time.time() - 7200
    os.utime(orphan, (old, old))

    fresh = base / "upl_fresh456"
    fresh.mkdir()
    (fresh / "chunk_0000").write_bytes(b"x" * 10)

    mgr = ChunkedUploadManager(base_dir=base)
    pruned = mgr.prune_stale(max_age_seconds=3600)
    assert pruned == 1, (
        f"F-15b: expected exactly the orphan dir pruned, got {pruned}"
    )
    assert not orphan.exists(), "orphan dir must be removed"
    assert fresh.exists(), "fresh dir must survive prune_stale"


# ── F-20: maintenance failure degrades /ready + Prometheus fixes ───────


def test_ready_after_failed_sweep():
    """F-20: a maintenance sweep with a failing task degrades /ready via
    maintenance_last_ok; a clean sweep restores readiness."""
    from relay_server.core.maintenance import MaintenanceScheduler
    from relay_server.core.metrics import evaluate_maintenance_results

    sched = MaintenanceScheduler()

    def _boom():
        raise RuntimeError("probe failure")

    sched.register("probe_task", _boom, 1)
    results = sched.run_due()

    ok = evaluate_maintenance_results(results)
    assert ok is False, f"failed sweep must evaluate to not-ok: {results}"
    metrics_mod.mark_maintenance_run(ok)

    body = client.get("/ready").json()
    assert body["status"] == "degraded", (
        f"F-20: /ready must degrade after a failed sweep, got {body}"
    )
    assert body["maintenance_last_ok"] is False
    assert body["scheduler"] == "stale"

    # Recovery: a clean sweep restores readiness.
    sched2 = MaintenanceScheduler()
    sched2.register("probe_ok", lambda: {"done": 1}, 1)
    results2 = sched2.run_due()
    ok2 = evaluate_maintenance_results(results2)
    assert ok2 is True
    metrics_mod.mark_maintenance_run(ok2)
    body2 = client.get("/ready").json()
    assert body2["status"] == "ready", (
        f"F-20: /ready must recover after a clean sweep, got {body2}"
    )
    assert body2["maintenance_last_ok"] is True


def test_histogram_has_inf_bucket():
    """F-20: _build_histogram emits a cumulative le=\"+Inf\" bucket equal
    to the observation count (Prometheus parser contract)."""
    hist = metrics_mod._build_histogram([7200.0], metrics_mod._LATENCY_BUCKETS)
    assert "+Inf" in hist["buckets"], (
        f"missing +Inf bucket: {hist['buckets']}"
    )
    assert hist["buckets"]["+Inf"] == 1

    metrics_mod.reset()
    text = metrics_mod.render_prometheus()
    assert 'le="+Inf"' in text, "rendered export must contain le=\"+Inf\""


def test_counter_label_escaping():
    """F-20: counter label values are escaped for the Prometheus text
    format (backslash, double quote, newline)."""
    metrics_mod.reset()
    metrics_mod.inc("relay_auth_failures_total", {"endpoint": '/quoted"path'})
    text = metrics_mod.render_prometheus()
    assert 'endpoint="/quoted\\"path"' in text, (
        "label value must be escaped in the rendered export"
    )
    assert 'endpoint="/quoted"path"' not in text