"""T-005g: Owner-Scoping der Artifact-Endpunkte (Card t_1df8a75c).

Non-Admins sehen/ändern bei /artifacts nur Artefakte eigener oder
ownerless Tasks; fremde Tasks melden sich als 404. storage_path wird
nie über die API ausgeliefert. Admins sehen alles (unchanged).
"""
import tempfile
from pathlib import Path

import pytest
from relay_server.config import settings
from relay_server.core.artifacts import list_artifacts, store_artifact
from relay_server.core.db import get_conn, init_db, q
from relay_server.core.events import event_bus
from relay_server.core.scheduler import Scheduler


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "test.db"
        settings.artifacts_dir = Path(tmp) / "artifacts"
        event_bus.clear()
        init_db()
        yield
        event_bus.clear()


def _seed_node(node_id: str) -> None:
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


def _make_task(task_name: str, owner: str | None) -> str:
    return Scheduler.create_task(
        task_name=task_name,
        stages=[{"stage_name": "s1", "capability": "generic", "depends_on": None,
                 "timeout_seconds": 60, "payload": {}}],
        owner_node_id=owner,
    )["task_id"]


def _artifact(task_id: str, name: str = "f.bin") -> dict:
    _seed_node("wk")
    return store_artifact(name=name, content=b"data", mime_type=None,
                          task_id=task_id, stage_id=None, created_by="wk")


# ── GET /artifacts/{task_id} scoping ──────────────────────────

class TestGetArtifactsScoping:
    def test_non_admin_foreign_task_hidden(self):
        _seed_node("alice"); _seed_node("bob")
        tid = _make_task("t-foreign", "bob")
        _artifact(tid)
        items = Scheduler.list_artifacts_scoped(tid, owner_node_id="alice")
        assert items == []

    def test_non_admin_own_task_visible(self):
        _seed_node("alice")
        tid = _make_task("t-own", "alice")
        _artifact(tid)
        items = Scheduler.list_artifacts_scoped(tid, owner_node_id="alice")
        assert len(items) == 1

    def test_ownerless_task_visible_to_all(self):
        _seed_node("alice")
        tid = _make_task("t-none", None)
        _artifact(tid)
        assert len(Scheduler.list_artifacts_scoped(tid, owner_node_id="alice")) == 1
        assert len(Scheduler.list_artifacts_scoped(tid, owner_node_id=None)) == 1

    def test_admin_sees_foreign_artifacts(self):
        _seed_node("alice"); _seed_node("bob")
        tid = _make_task("t-foreign", "bob")
        _artifact(tid)
        assert len(Scheduler.list_artifacts_scoped(tid, owner_node_id=None)) == 1


# ── DELETE /artifacts/{artifact_id} scoping ───────────────────

class TestDeleteArtifactScoping:
    def test_non_admin_cannot_delete_foreign(self):
        _seed_node("alice"); _seed_node("bob")
        tid = _make_task("t-foreign", "bob")
        art = _artifact(tid)
        assert Scheduler.delete_artifact_scoped(art["artifact_id"], owner_node_id="alice") is False

    def test_non_admin_can_delete_own(self):
        _seed_node("alice")
        tid = _make_task("t-own", "alice")
        art = _artifact(tid)
        assert Scheduler.delete_artifact_scoped(art["artifact_id"], owner_node_id="alice") is True

    def test_ownerless_deletable_by_any(self):
        _seed_node("alice")
        tid = _make_task("t-none", None)
        art = _artifact(tid)
        assert Scheduler.delete_artifact_scoped(art["artifact_id"], owner_node_id="alice") is True

    def test_admin_can_delete_foreign(self):
        _seed_node("alice"); _seed_node("bob")
        tid = _make_task("t-foreign", "bob")
        art = _artifact(tid)
        assert Scheduler.delete_artifact_scoped(art["artifact_id"], owner_node_id=None) is True


# ── POST owner-check (Scheduler.artifact_task_owner) ──────────

class TestUploadOwnerCheck:
    def test_foreign_owner_detected(self):
        _seed_node("alice"); _seed_node("bob")
        tid = _make_task("t-foreign", "bob")
        assert Scheduler.artifact_task_owner(tid) == "bob"

    def test_ownerless_and_missing_both_none(self):
        _seed_node("alice")
        tid = _make_task("t-none", None)
        assert Scheduler.artifact_task_owner(tid) is None
        assert Scheduler.artifact_task_owner("t-missing") is None


# ── storage_path never in API responses ───────────────────────

class TestStoragePathLeakage:
    def test_list_response_has_no_storage_path(self):
        _seed_node("alice")
        tid = _make_task("t-own", "alice")
        _artifact(tid)
        items = list_artifacts(task_id=tid)
        for it in items:
            assert "storage_path" not in it

    def test_scoped_response_has_no_storage_path(self):
        _seed_node("alice")
        tid = _make_task("t-own", "alice")
        _artifact(tid)
        items = Scheduler.list_artifacts_scoped(tid, owner_node_id="alice")
        for it in items:
            assert "storage_path" not in it

    def test_get_task_artifacts_no_storage_path(self):
        _seed_node("alice")
        tid = _make_task("t-own", "alice")
        _artifact(tid)
        detail = Scheduler.get_task(tid)
        for it in detail["artifacts"]:
            assert "storage_path" not in it