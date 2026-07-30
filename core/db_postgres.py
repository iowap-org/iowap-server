"""PostgreSQL backend for the relay server.

Status: **stub**. The class implements the :class:`relay_server.core.db.Database`
interface but every method raises :class:`NotImplementedError` with a clear
message. The full implementation (asyncpg connection pool, ``$N``
placeholders, backend-specific schema + migrations) is tracked separately.

Once implemented, business logic needs **no changes** — it already goes
through ``db.get_conn()`` / ``db.init_db()`` on the active singleton, which
``create_database()`` wires to this class when ``settings.db_type == "postgres"``.
"""

from typing import Any

from relay_server.core.db import Database


class PostgresDatabase(Database):
    """PostgreSQL backend — asyncpg-based. Not yet implemented."""

    def __init__(self, dsn: str):
        self.dsn = dsn

    def get_conn(self) -> Any:
        raise NotImplementedError(
            "PostgreSQL support is not yet implemented. "
            "The Database interface is in place; the asyncpg-backed driver, "
            "schema, and migrations are still pending. "
            "Set db_type: sqlite in config.yaml to keep running."
        )

    def init_db(self) -> None:
        raise NotImplementedError(
            "PostgreSQL support is not yet implemented. "
            "Set db_type: sqlite in config.yaml to keep running."
        )

    def close(self) -> None:
        pass