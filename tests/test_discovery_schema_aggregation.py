"""T-204: Discovery schema/description aggregation — acceptance tests (RED first).

Same order-dependency class as T-004 (result_path_hints) / T-195 (available),
but for ``input_schema`` and ``description`` in
``core/discovery.get_capabilities``:

The first provider creating a ``cap_map`` entry won ``input_schema`` and
``description`` unconditionally. A schema-less provider iterated first
(e.g. NovaForge heartbeating a bare capability) permanently blanked the
aggregate for a capability another provider correctly declares with
``input_schema.fields`` â dashboard forms rendered an empty Advanced-JSON
textarea for caps that HAVE field schemas.

SOLL: optional metadata aggregates first-non-None-wins (schema) /
first-non-empty-wins (description), mirroring the T-004 hints rule.
Result must be order-independent.
"""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

os.environ["RELAY_DB_PATH"] = ""

from relay_server.config import settings
from relay_server.core.db import get_conn, init_db, q, sync_node_capabilities
from relay_server.core.discovery import get_capabilities

SCHEMA_B = {"fields": {"prompt": {"name": "prompt", "type": "string", "required": True}}}


@pytest.fixture(autouse=True)
def fresh_db():
    with tempfile.TemporaryDirectory() as tmp:
        settings.db_path = Path(tmp) / "test.db"
        init_db()
        yield


def _seed_node(node_id: str, name: str, caps: list, load: float) -> None:
    """Seed a live node row + normalized capability index (T-195 lesson:
    last_seen = now, load pins the ORDER BY loop order, sync after the
    INSERT connection is closed)."""
    now = datetime.now(UTC).isoformat()
    conn = get_conn()
    conn.execute(
        q(
            "INSERT INTO nodes (node_id, node_name, capabilities, load, "
            "queue_depth, available, last_seen, registered_at, status, role) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (node_id, name, json.dumps(caps), load, 0, True, now, now,
             "online", "node"),
        )
    )
    conn.commit()
    conn.close()
    sync_node_capabilities(node_id, caps)


def _cap(schema=None, description=""):
    return {
        "name": "dual.cap",
        "type": "tool",
        "version": "1.0.0",
        "description": description,
        "available": True,
        **({"input_schema": schema} if schema is not None else {}),
    }


def test_schema_of_declaring_provider_survives_order():
    """Schema-less provider iterated FIRST must not blank the aggregate."""
    _seed_node("n_a", "schemaless-first", [_cap()], load=0.0)
    _seed_node("n_b", "declaring-second", [_cap(schema=SCHEMA_B, description="Real desc")], load=5.0)

    agg = {c["name"]: c for c in get_capabilities(available_only=False)}["dual.cap"]
    assert agg["input_schema"] == SCHEMA_B, (
        "aggregate input_schema must come from the declaring provider, "
        "even when a schema-less provider is iterated first"
    )
    assert agg["description"] == "Real desc"


def test_reverse_order_same_result():
    """Order-independence invariant: reversed iteration, same aggregate."""
    _seed_node("n_b", "declaring-first", [_cap(schema=SCHEMA_B, description="Real desc")], load=0.0)
    _seed_node("n_a", "schemaless-second", [_cap()], load=5.0)

    agg = {c["name"]: c for c in get_capabilities(available_only=False)}["dual.cap"]
    assert agg["input_schema"] == SCHEMA_B
    assert agg["description"] == "Real desc"


def test_schema_wins_only_until_filled():
    """A second declaring provider must NOT overwrite an already-set schema."""
    schema_c = {"fields": {"other": {"name": "other", "type": "string"}}}
    _seed_node("n_b", "declaring-first", [_cap(schema=SCHEMA_B)], load=0.0)
    _seed_node("n_c", "declaring-second-other", [_cap(schema=schema_c)], load=5.0)

    agg = {c["name"]: c for c in get_capabilities(available_only=False)}["dual.cap"]
    assert agg["input_schema"] == SCHEMA_B