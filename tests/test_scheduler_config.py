"""T-181: claim_ttl_seconds + max_retries — validators, DB overrides, adaptive watchdog.

The 300s stage timeout was dead code (never a watchdog) and was removed;
the claim TTL is the only claim timer now. These settings are
dashboard-configurable via settings_override (T-164/T-165 pattern) and
changes must apply without a restart (adaptive, Ronny's decision).
"""

import os
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

os.environ["RELAY_DB_PATH"] = ""

from relay_server.config import Settings, settings
from relay_server.core.db import (
    apply_settings_overrides,
    get_conn,
    init_db,
    q,
    set_settings_override,
)


@pytest.fixture(autouse=True)
def fresh_db():
    """Temp DB per test; snapshot + restore the global settings object.

    Note: db_path is deliberately NOT restored — the boot pattern
    (RELAY_DB_PATH="" before import) leaves it as Path("") == ".", which
    is not a valid SQLite path. Each test re-points it into its own
    tempdir anyway; restoring the broken value would make any post-yield
    DB access fail with "unable to open database file".
    """
    snapshot = {k: v for k, v in settings.model_dump().items() if k != "db_path"}
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "test.db"
        init_db()
        yield
        # Teardown runs INSIDE the with-block — the temp DB still exists.
        conn = get_conn()
        conn.execute(q("DELETE FROM settings_override"))
        conn.commit()
        conn.close()
    # Restore every mutated field except db_path (see note above).
    for k, v in snapshot.items():
        setattr(settings, k, v)


def test_claim_ttl_range_validators():
    with pytest.raises(ValidationError):
        Settings(claim_ttl_seconds=59)
    with pytest.raises(ValidationError):
        Settings(claim_ttl_seconds=301)
    assert Settings(claim_ttl_seconds=60).claim_ttl_seconds == 60
    assert Settings(claim_ttl_seconds=300).claim_ttl_seconds == 300


def test_max_retries_range_validators():
    with pytest.raises(ValidationError):
        Settings(max_retries=-1)
    with pytest.raises(ValidationError):
        Settings(max_retries=11)
    assert Settings(max_retries=0).max_retries == 0
    assert Settings(max_retries=10).max_retries == 10

def test_claim_settings_db_override_applies():
    set_settings_override("claim_ttl_seconds", "180")
    set_settings_override("max_retries", "5")
    apply_settings_overrides()
    assert settings.claim_ttl_seconds == 180
    assert settings.max_retries == 5


def test_claim_settings_db_override_rejected_out_of_range():
    # The setter does not range-check; apply re-validates via the Pydantic
    # validators. An out-of-range row is silently SKIPPED (keep old value)
    # — same failure mode as the T-164 ladder constraints: a corrupt row
    # must never crash the server. The dashboard endpoint range-checks
    # before writing, so bad values normally never reach the table.
    set_settings_override("claim_ttl_seconds", "30")
    apply_settings_overrides()
    assert settings.claim_ttl_seconds == 60  # unchanged (default)


def test_watchdog_interval_adapts_without_restart():
    from relay_server.core.maintenance import maintenance_scheduler

    maintenance_scheduler.register_defaults()
    assert (
        maintenance_scheduler._tasks["claim_ttl_watchdog"]["interval"]
        == settings.claim_ttl_seconds
    )

    set_settings_override("claim_ttl_seconds", "180")
    apply_settings_overrides()
    assert settings.claim_ttl_seconds == 180
    # The watchdog must pick up the new interval WITHOUT a restart.
    assert maintenance_scheduler._tasks["claim_ttl_watchdog"]["interval"] == 180
