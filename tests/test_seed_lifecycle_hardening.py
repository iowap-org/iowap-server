"""QA-Audit seed lifecycle (external report 2026-10-17).

Before this change, two spots lied about the master seed:

1. ``relay-server admin init-master`` exits 1 ("Master admin seed already
   exists.") when the DB row wins over a freshly written config seed — but
   consumers cannot rely on that contract because it is not locked by a test.
   The installer decided SEED_FLAG before init-master ever ran, so on a
   wiped-config re-install it printed a brand-new seed whose login attempt
   ends in "Invalid master seed" (the old admin_seeds hash still governs).

2. After the first human admin was created, the plaintext master_seed value
   stayed in config.yaml although seed login is closed from that point on
   (has_admin_user() gate). Anyone with read access to config.yaml therefore
   held the recovery credential (enable_master_seed_login=true) without being
   the operator it belongs to.

These tests lock the contract the installer depends on (test 1) and purge the
dead plaintext seed server-side at bootstrap (test 2).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

os.environ["RELAY_DB_PATH"] = ""

from relay_server.config import settings  # noqa: E402
from relay_server.core.db import init_db, q  # noqa: E402
from relay_server.core.session import generate_csrf_token, sign_user_cookie  # noqa: E402
from relay_server.main import app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]  # noqa: E402

BASE = "/relay/v2/dashboard"


# ---------------------------------------------------------------------------
# Test 1 — the CLI contract install.sh must rely on (DB row wins over config)
# ---------------------------------------------------------------------------

def _run_init_master(config_value: str, tmp: Path, db_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "RELAY_DB_PATH": str(db_path),
        "RELAY_CONFIG_PATH": str(tmp / "config.yaml"),
        "PYTHONPATH": str(REPO_ROOT),
    }
    cfg = {"db_type": "sqlite", "session_secret": "test-secret", "master_seed": config_value}
    Path(env["RELAY_CONFIG_PATH"]).write_text(yaml.safe_dump(cfg, sort_keys=False))
    return subprocess.run(
        [sys.executable, "-m", "relay_server.main", "admin", "init-master"],
        capture_output=True, text=True, env=env, timeout=30,
        cwd=str(REPO_ROOT),
    )


def test_init_master_db_row_wins_over_config_seed():
    """Fresh DB + config seed -> rc 0 + SECRET line.

    Then a REPLACED config seed (simulating the installer's wiped-config
    re-run) must NOT succeed again: rc 1, stderr names "already exists".
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "server.db"

        # (a) fresh install: the printed seed is the credential
        first = _run_init_master("seedvalue-original", Path(tmp), db)
        assert first.returncode == 0, first.stderr
        assert "SECRET:" in first.stdout

        # (b) install.sh re-run with wiped config -> new seed in config,
        #     but the DB row was created with the ORIGINAL secret
        second = _run_init_master("seedvalue-regenerated", Path(tmp), db)
        assert second.returncode == 1
        assert "already exists" in second.stderr


# ---------------------------------------------------------------------------
# Test 2 — bootstrap purges the dead plaintext master seed from config.yaml
# ---------------------------------------------------------------------------

client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


@pytest.fixture()
def seeded_env():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        settings.db_path = tmp_path / "t_seed.db"
        settings.session_cookie_secure = False
        settings.session_secret = "test-secret"
        settings.config_path = tmp_path / "config.yaml"
        settings.master_seed = "purge-me-please"
        settings.enable_master_seed_login = False
        cfg = {
            "db_type": "sqlite",
            "session_secret": "test-secret",
            "master_seed": "purge-me-please",
            "unrelated_setting": "must-survive",
        }
        settings.config_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        init_db()
        seed = (  # replicate init_master_seed() row so login state is realistic
            "seedrow"
        )
        from relay_server.core.auth import init_master_seed
        init_master_seed()
        yield {"tmp": tmp_path, "marker": seed}
        # restore globals mutated by other test files' fixtures
        settings.config_path = None


def test_bootstrap_purges_master_seed_from_config(seeded_env):
    """POST /api/bootstrap creates the first admin AND removes master_seed
    from config.yaml — but keeps every other key."""
    info = seeded_env
    client.cookies.set(
        "relay_user",
        sign_user_cookie({"user_id": "__master__", "username": "master"}),
        domain="testserver.local",
    )
    token = generate_csrf_token()
    client.cookies.set("relay_csrf", token, domain="testserver.local")

    resp = client.post(
        f"{BASE}/api/bootstrap",
        data={"username": "ronny", "email": "ronny@example.org"},
        headers={"X-CSRF-Token": client.cookies.get("relay_csrf", domain="testserver.local")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"

    # the dead plaintext seed is gone
    cfg = yaml.safe_load(info["tmp"].joinpath("config.yaml").read_text())
    assert "master_seed" not in cfg
    assert cfg["unrelated_setting"] == "must-survive"

    # and the in-memory setting no longer carries it either
    from relay_server.config import settings as live_settings
    assert live_settings.master_seed is None