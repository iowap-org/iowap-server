"""PostgreSQL backend for the relay server (T-110).

Backed by a SQLAlchemy Core engine. The schema is created from the
portable :data:`relay_server.core.tables.metadata` (``create_all`` works on
PostgreSQL), and the migrations run through the same backend-aware
:func:`relay_server.core.db._run_migrations` helper (``information_schema``
introspection).

Business logic needs **no changes** — it already goes through
``db.get_conn()`` / ``db.init_db()`` on the active singleton, which
:func:`relay_server.core.db.create_database` wires to this class when
``settings.db_type == "postgres"``.
"""

from __future__ import annotations

from typing import Optional

import sqlalchemy as sa

from relay_server.core import tables


class PostgresDatabase:
    """PostgreSQL backend — SQLAlchemy Core engine with connection pooling.

    The DSN format is the SQLAlchemy URL form, e.g.
    ``postgresql+psycopg://user:pass@host:5432/relay``.

    NOTE: this intentionally does NOT subclass ``relay_server.core.db.Database``
    at import time. ``db.py`` imports this module lazily inside
    ``create_database()`` while ``db.py`` is still initialising; a module-level
    ``from relay_server.core.db import Database`` here creates a circular
    import. The helpers (``_seed_default_rbac``/``_run_migrations``) are
    imported lazily in ``init_db`` for the same reason. Duck-typing keeps
    compatibility with the ``Database`` interface used by ``create_database``.
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._engine: Optional[sa.Engine] = None

    def _get_engine(self) -> sa.Engine:
        if self._engine is None:
            self._engine = sa.create_engine(
                self._dsn,
                pool_pre_ping=True,
                future=True,
            )
        return self._engine

    def get_conn(self) -> sa.engine.Connection:
        """Open a SQLAlchemy connection from the pool."""
        return self._get_engine().connect()

    def init_db(self) -> None:
        """Create the schema from the portable metadata, seed defaults, migrate.

        Uses ``metadata.create_all`` (works on PostgreSQL without dialect-
        specific DDL) instead of the raw ``_schema`` DDL strings, which carry
        SQLite-isms (``AUTOINCREMENT``, ``strftime`` defaults). The RBAC
        seeds and the backend-aware migrations run through the shared
        helpers so they stay in sync with the SQLite path.
        """
        engine = self._get_engine()
        with engine.begin() as conn:
            tables.metadata.create_all(conn)
            # Lazy import breaks the circular dependency: db.py imports this
            # module during create_database(), which runs while db.py is still
            # being initialised.
            from relay_server.core.db import _run_migrations, _seed_default_rbac

            _seed_default_rbac(conn)
            _run_migrations(conn)

    def close(self) -> None:
        """Dispose the engine and release the pool."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None