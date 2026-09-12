"""T-187: versioned schema migrations — acceptance tests (RED against unmodified main).

Covers F-09 (violates S-05.3) / decision D-9: a ``schema_version`` table plus
numbered migrations, with the legacy introspection cascade left in place as the
first-boot baseline path.

Contract (design: ``.hermes/pipeline/t187/design.md``):

* ``MIGRATIONS`` is an ordered list of ``(version, name, callable)`` entries;
  versions are strictly increasing positive integers.
* ``apply_migrations(conn)`` creates ``schema_version``, applies every migration
  whose version is above the recorded ``MAX(version)`` and records one row per
  applied migration (``version``, ``name``, ``applied_at``).
* A database **without** a version row replays *all* migrations (their bodies are
  idempotent) and is stamped at the latest version — that is the baseline path
  for both a fresh and a grown database.
* A database whose recorded version is **ahead** of the code is left untouched.

Fixture pattern follows ``tests/test_artifact_observability.py`` (fresh temp DB
per test via ``settings.db_path`` + ``init_db()``, ``RELAY_DB_PATH`` reset).
"""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

os.environ["RELAY_DB_PATH"] = ""

from relay_server.config import settings
from relay_server.core.db import MIGRATIONS, apply_migrations, get_conn, init_db, q

LATEST = MIGRATIONS[-1][0]


@pytest.fixture(autouse=True)
def fresh_db():
    """Temporary database per test, initialised through the normal boot path."""
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "test.db"
        init_db()
        yield Path(tmp)


def _rows(sql: str, params=()):
    conn = get_conn()
    return conn.execute(q(sql, params)).fetchall()


def _versions() -> list:
    return [r[0] for r in _rows("SELECT version FROM schema_version ORDER BY version")]


def _raw_edit(*statements: str) -> None:
    """Apply DDL/DML sidestepping the server connection (legacy-DB simulation)."""
    raw = sqlite3.connect(str(settings.db_path))
    try:
        for stmt in statements:
            raw.execute(stmt)
        raw.commit()
    finally:
        raw.close()


def _node_columns() -> list:
    conn = get_conn()
    return [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(nodes)").fetchall()]


def test_schema_version_table_is_populated_on_fresh_db():
    rows = _rows("SELECT version, name, applied_at FROM schema_version ORDER BY version")
    assert len(rows) == len(MIGRATIONS)
    assert rows[-1][0] == LATEST


def test_versions_are_strictly_increasing_and_named():
    versions = [version for version, _name, _fn in MIGRATIONS]
    assert all(isinstance(v, int) and v > 0 for v in versions)
    assert versions == sorted(set(versions))

    rows = _rows("SELECT version, name, applied_at FROM schema_version ORDER BY version")
    assert [(r[0], r[1]) for r in rows] == [(v, n) for v, n, _fn in MIGRATIONS]
    assert all(r[2] for r in rows), "every applied migration carries a timestamp"


def test_migration_records_are_ordered():
    assert _versions() == [version for version, _name, _fn in MIGRATIONS]


def test_second_init_is_idempotent():
    before = _rows("SELECT version, name, applied_at FROM schema_version ORDER BY version")
    init_db()
    init_db()
    after = _rows("SELECT version, name, applied_at FROM schema_version ORDER BY version")
    assert after == before, "a second boot must neither re-run nor re-record migrations"


def test_legacy_db_without_version_rows_is_baselined_and_keeps_data():
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO settings_override (key, value, updated_at) VALUES (?, ?, ?)",
            ("max_inline_bytes", "1234", "2026-09-12T00:00:00+00:00"),
        )
    )
    conn.commit()

    # A database grown before T-187 has the schema but no version history.
    _raw_edit("DELETE FROM schema_version")
    assert _versions() == []

    init_db()

    assert _versions() == [version for version, _name, _fn in MIGRATIONS]
    kept = _rows("SELECT value FROM settings_override WHERE key = ?", ("max_inline_bytes",))
    assert [r[0] for r in kept] == ["1234"], "baseline stamping must not touch existing data"


def test_legacy_schema_missing_column_gets_migrated():
    assert "consecutive_high_load" in _node_columns()

    # Simulate a pre-T-081 database: column gone, no version history.
    _raw_edit(
        "ALTER TABLE nodes DROP COLUMN consecutive_high_load",
        "DELETE FROM schema_version",
    )
    assert "consecutive_high_load" not in _node_columns()

    init_db()

    assert "consecutive_high_load" in _node_columns(), "missing column must be re-added"
    assert _versions() == [version for version, _name, _fn in MIGRATIONS]


def test_newer_db_version_is_not_downgraded():
    _raw_edit(
        "DELETE FROM schema_version",
        "INSERT INTO schema_version (version, name, applied_at) "
        "VALUES (999, 'from-the-future', '2999-01-01T00:00:00+00:00')",
    )

    init_db()

    assert _versions() == [999], "a database ahead of the code must be left untouched"


def test_apply_migrations_is_callable_on_live_connection():
    conn = get_conn()
    assert apply_migrations(conn) == LATEST
    conn.commit()
