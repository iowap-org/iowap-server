"""T-184 + T-186: SSN removal and MariaDB stub removal — acceptance tests.

Two pure removals (no new behavior), audit findings F-21 (S-10.1/S-10.3)
and F-23 (S-05.1, decision D-6):

T-184 — the Server-Side Node capability-page machinery is gone:
* ``relay_server.config.settings`` has no ``ssn_*`` attributes
* ``relay_server.main`` has no ``_ssn_start`` / ``_ssn_stop`` and no
  leftover ``subprocess`` import (its only user was the SSN helpers)
* ``relay_server.core.maintenance`` has no ``_ssn_auto_approve`` and its
  default registry never registers an ``ssn_auto_approve`` task
* ``GET /relay/v2/dashboard/api/ssn-pages`` returns 404 (unauthenticated
  *and* with an admin session — otherwise the route could hide behind
  the auth gate)
* the dashboard endpoint catalog has no SSN entries
* the admin UI no longer fetches/renders capability pages, while the
  node-profile overlay (T-092) keeps working under its own ids
* no server source file still references SSN (S-10.3)

T-186 — MariaDB is no longer a selectable backend:
* ``db_type="mariadb"`` is rejected by the Settings validation
* ``mariadb_dsn`` does not exist
* ``create_database()`` with ``db_type="mariadb"`` fails (ValueError)
* ``relay_server.core.db_mariadb`` is not importable
* stale MariaDB comments in db.py / tables.py / users.py / cluster.py
  are cleaned up (decision D-3)
* SQLite and PostgreSQL stay valid selections (invariants)

Test style follows tests/test_dashboard_auth_details.py: an autouse
``fresh_db`` fixture with a temp DB, a module-level TestClient, and
helpers that plant cookies with ``domain="testserver.local"`` (the
httpx single-label-host quirk, see T-193 spike notes).

RED expectations on unmodified HEAD `7643981`: **19 RED / 4 invariant**.
The 19 removal tests assert the absence of code that still exists, so
each fails with AttributeError / import success / succeeded-request /
missing-exception on HEAD and only turns green once the removal edits
land. The overlay and modal-CSS tests are RED on HEAD too (they assert
the renamed node-profile ids/classes). The 4 invariants pass on HEAD
and must stay green: ``test_ssn_page_single_route_is_gone`` (route
removed in T-076 already) and the three backend-selection invariants
(sqlite/postgres valid, unknown type still rejected).
"""

import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

os.environ["RELAY_DB_PATH"] = ""

from relay_server.config import Settings, settings
from relay_server.core.session import sign_user_cookie
from relay_server.main import app

BASE = "/relay/v2/dashboard"
SSN_PAGES_URL = f"{BASE}/api/ssn-pages"

REPO_ROOT = Path(__file__).resolve().parents[1]

# Source files scanned by test_no_ssn_in_server_source / the MariaDB
# comment tests. Everything under relay_server except tests: docs/ is a
# submodule and board/ is gitignored board context (both never shipped);
# static/ .js/.html/.css ARE scanned (admin.js, node-profile.js,
# admin.css, cluster.css carry the SSN references being removed).
_SCAN_EXCLUDE_DIRS = {".venv", ".hermes", "docs", "board", "tests", "__pycache__",
                      ".git", ".ruff_cache", ".pytest_cache", "tools"}


_SCAN_SUFFIXES = {".py", ".js", ".html", ".css"}


def _scan_source_files(needle: str) -> list[str]:
    """Return source files under the repo (excl. tests/docs/tools/board)
    whose text contains ``needle`` (case-insensitive).

    ``className`` is filtered out before matching: lowercased it
    contains the substring ``ssn`` (cla*ssn*ame) and would produce a
    false positive in every static JS file.
    """
    offenders: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
            continue
        rel = path.relative_to(REPO_ROOT)
        if any(part in _SCAN_EXCLUDE_DIRS or part.endswith(".egg-info")
               for part in rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except UnicodeDecodeError:
            continue
        if needle in text.replace("classname", ""):
            offenders.append(str(rel))
    return offenders


@pytest.fixture(autouse=True)
def fresh_db():
    """Use a temporary database for each test (T-193 fixture pattern)."""
    from relay_server.core.db import init_db

    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "t184.db"
        settings.heartbeat_interval_seconds = 1
        settings.heartbeat_timeout_multiplier = 1
        settings.session_cookie_secure = False
        init_db()
        yield


client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def _admin_session() -> None:
    """Plant a master-seed dashboard session cookie (T-193 pattern)."""
    client.cookies.set(
        "relay_user",
        sign_user_cookie({"user_id": "__master__", "username": "master"}),
        domain="testserver.local",
    )


# ── T-184: SSN removal (F-21, S-10.1/S-10.3) ─────────────────────────────


def test_settings_has_no_ssn_attributes():
    """S-10.1: the ``ssn_*`` settings block must be gone from config.py."""
    ssn_keys = sorted(k for k in settings.model_dump() if k.startswith("ssn_"))
    assert not ssn_keys, f"ssn_* settings still present: {ssn_keys}"


def test_main_has_no_ssn_start_stop():
    """S-10.1: main.py must not define the SSN lifespan helpers."""
    import relay_server.main as main_mod

    assert not hasattr(main_mod, "_ssn_start"), "main._ssn_start still exists"
    assert not hasattr(main_mod, "_ssn_stop"), "main._ssn_stop still exists"


def test_main_imports_no_subprocess():
    """S-10.1: main.py's only subprocess user was the SSN helpers."""
    import relay_server.main as main_mod

    assert not hasattr(main_mod, "subprocess"), (
        "main.py still imports subprocess — its only user was "
        "_ssn_start/_ssn_stop"
    )


def test_maintenance_has_no_ssn_auto_approve():
    """S-10.1: core/maintenance.py must not define the SSN watchdog."""
    import relay_server.core.maintenance as maint

    assert not hasattr(maint, "_ssn_auto_approve"), (
        "maintenance._ssn_auto_approve still exists"
    )


def test_maintenance_registry_has_no_ssn_task():
    """S-10.1: register_defaults() must never register an SSN task.

    Flag-independent: while the ``ssn_*`` settings exist (HEAD), the
    registry branch is probed with the flags forced on; after the
    removal the settings are gone and the task cannot be registered
    either way (the hasattr guard keeps the test runnable in both
    states and the final assertion is the removal proof).
    """
    from relay_server.core.maintenance import MaintenanceScheduler

    sched = MaintenanceScheduler()
    if hasattr(settings, "ssn_enabled"):
        old_enabled, old_auto = settings.ssn_enabled, settings.ssn_auto_approve
        try:
            settings.ssn_enabled = True
            settings.ssn_auto_approve = True
            sched.register_defaults()
        finally:
            settings.ssn_enabled = old_enabled
            settings.ssn_auto_approve = old_auto
    else:
        sched.register_defaults()
    names = {t["name"] for t in sched.status()}
    assert "ssn_auto_approve" not in names, (
        f"maintenance registry still contains ssn_auto_approve: {sorted(names)}"
    )


def test_ssn_pages_endpoint_returns_404():
    """S-10.1: the SSN pages listing route must be gone.

    On HEAD an unauthenticated GET is rejected with 401 *before*
    routing, so this test alone cannot prove the route is gone.
    """
    r = client.get(SSN_PAGES_URL)
    assert r.status_code == 404, (
        f"GET {SSN_PAGES_URL} returned {r.status_code} unauthenticated"
    )


def test_ssn_pages_endpoint_404_with_admin_session():
    """S-10.1: the removal must hold behind the auth gate (HEAD: 200)."""
    _admin_session()
    r = client.get(SSN_PAGES_URL)
    assert r.status_code == 404, (
        f"GET {SSN_PAGES_URL} returned {r.status_code} with an admin "
        "session — the SSN pages route must be removed"
    )


def test_ssn_page_single_route_is_gone():
    """S-10.3: the removed ``/api/ssn-page/{capability}`` route must stay
    gone (it was already removed in T-076; this pins the absence)."""
    r = client.get(f"{BASE}/api/ssn-page/some.capability")
    assert r.status_code == 404


def test_endpoints_catalog_has_no_ssn_entries():
    """S-10.1: the dashboard endpoint catalog must drop the SSN entries."""
    _admin_session()
    r = client.get(f"{BASE}/api/endpoints")
    assert r.status_code == 200
    paths = {e["path"] for e in r.json()["endpoints"]}
    ssn_paths = sorted(p for p in paths if "ssn" in p.lower())
    assert not ssn_paths, f"endpoint catalog still lists SSN routes: {ssn_paths}"


def test_admin_js_has_no_ssn_fetch():
    """S-10.3: the admin UI must not call the removed endpoint."""
    admin_js = (REPO_ROOT / "static" / "admin.js").read_text(encoding="utf-8")
    for stale in (
        "/api/ssn-pages",
        "ssnPageCapabilities",
        "loadSsnPages",
        "openSsnPageModal",
        "hideSsnPageModal",
        "cap-page-badge",
        "cap-card-clickable",
    ):
        assert stale not in admin_js, f"admin.js still contains {stale!r}"


def test_admin_html_has_no_ssn_ids():
    """S-10.3: the SSN modal ids must be gone from admin.html."""
    admin_html = (REPO_ROOT / "static" / "admin.html").read_text(encoding="utf-8")
    for stale in ("ssnPageOverlay", "ssnPageBox", "ssnPageFrame", "ssnPageTitle",
                  "close-ssn-page-btn", "ssn-box"):
        assert stale not in admin_html, f"admin.html still contains {stale!r}"


def test_admin_overlay_still_serves_node_profiles():
    """T-092 invariant (green on HEAD and after the removal): the overlay
    keeps working for node profiles under its own (renamed) ids."""
    admin_js = (REPO_ROOT / "static" / "admin.js").read_text(encoding="utf-8")
    admin_html = (REPO_ROOT / "static" / "admin.html").read_text(encoding="utf-8")
    for needed in ("nodePageOverlay", "nodePageBox", "nodePageFrame", "nodePageTitle"):
        assert needed in admin_js, f"admin.js lost the node-profile overlay id {needed}"
        assert f'id="{needed}"' in admin_html, (
            f"admin.html lost the node-profile overlay id {needed}"
        )
    assert "/relay/v2/dashboard/node/" in admin_js, (
        "admin.js lost the node-profile overlay navigation"
    )


def test_admin_css_modal_renamed():
    """S-10.3: the admin modal CSS must serve the node-profile overlay
    under its renamed class, with no SSN selectors left."""
    admin_css = (REPO_ROOT / "static" / "admin.css").read_text(encoding="utf-8")
    for stale in ("ssn-box", "#ssnPageFrame", ".close-ssn-page-btn",
                  "banner.ssn", "node-avatar.ssn"):
        assert stale not in admin_css, f"admin.css still contains {stale!r}"
    assert "node-box" in admin_css, "admin.css lost the node-profile modal class"
    assert "#nodePageFrame" in admin_css, "admin.css lost the iframe selector"


def test_no_ssn_in_server_source():
    """S-10.3: no server-side source file may reference SSN anymore.

    Includes the name-keyed node cosmetics (banner/avatar classes,
    emoji/avatar helpers) in admin.js, node-profile.js, admin.css and
    cluster.css — they key on a node *named* "…ssn…", not on SSN
    machinery, but they are the last SSN tokens in the repo and leave
    with the feature (design decision D-5).
    """
    offenders = _scan_source_files("ssn")
    assert not offenders, f"SSN references remain in server source: {offenders}"


# ── T-186: MariaDB stub removal (F-23, S-05.1, D-6) ──────────────────────


def test_db_type_literal_rejects_mariadb():
    """S-05.1: ``mariadb`` must be rejected by the Settings validation."""
    with pytest.raises(ValidationError):
        Settings(db_type="mariadb")


def test_mariadb_dsn_is_gone():
    """S-05.1: the ``mariadb_dsn`` setting must not exist."""
    assert "mariadb_dsn" not in Settings.model_fields, (
        "Settings still declares mariadb_dsn"
    )
    assert not hasattr(settings, "mariadb_dsn"), "settings still carries mariadb_dsn"


def test_db_mariadb_module_not_importable():
    """S-05.1: the stub module must be deleted."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("relay_server.core.db_mariadb")


def test_create_database_rejects_mariadb():
    """S-05.1: the factory must fail for ``db_type="mariadb"`` (was: it
    returned the MariadbDatabase stub). The explicit unknown-backend
    ValueError must be raised, before any driver import."""
    import relay_server.core.db as db_mod

    old_type = settings.db_type
    try:
        settings.db_type = "mariadb"
        with pytest.raises(ValueError, match=r"[Uu]nknown db_type"):
            db_mod.create_database()
    finally:
        settings.db_type = old_type


def test_db_py_has_no_mariadb_mentions():
    """S-05.1: stale MariaDB mentions in db.py must be cleaned up
    (docstring backend list, factory branch, q() comment)."""
    db_text = (REPO_ROOT / "core" / "db.py").read_text(encoding="utf-8")
    assert "mariadb" not in db_text.lower(), "core/db.py still mentions MariaDB"


def test_stale_mariadb_comments_cleaned():
    """S-05.1: the audit's stale-comment list must be cleaned (D-3)."""
    for rel in ("core/tables.py", "core/users.py", "api/v2/cluster.py", "config.py"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "mariadb" not in text.lower(), f"{rel} still mentions MariaDB"


def test_sqlite_still_valid():
    """Invariant: SQLite remains a valid backend selection."""
    assert Settings(db_type="sqlite").db_type == "sqlite"


def test_postgres_still_valid():
    """Invariant: PostgreSQL remains a valid backend selection."""
    assert Settings(db_type="postgres").db_type == "postgres"


def test_unknown_db_type_still_rejected():
    """Invariant: the factory's unknown-backend ValueError stays."""
    import relay_server.core.db as db_mod

    old_type = settings.db_type
    try:
        settings.db_type = "oracle"
        with pytest.raises(ValueError, match=r"[Uu]nknown db_type"):
            db_mod.create_database()
    finally:
        settings.db_type = old_type