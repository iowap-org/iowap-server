"""Cluster-side node ID minting and uniqueness registry.

ADR-001: node IDs are 8-character strings from an unambiguous alphanumeric
alphabet.  The cluster mints them, never the client.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from relay_server.core.db import get_conn, sync_node_capabilities

NODE_ID_ALPHABET = "ABCDEFGHJKMNPQRTUVWXY346789"
NODE_ID_LENGTH = 8
NODE_ID_MAX_RETRIES = 100


class NodeRegistryError(Exception):
    """Base exception for node registry failures."""


class NodeNameExistsError(NodeRegistryError):
    """Raised when a node with the requested name is already registered."""


class NodeIdExhaustedError(NodeRegistryError):
    """Raised when no unused node ID can be allocated."""


def normalize_node_id(node_id: str) -> str:
    """Return the canonical uppercase form of a node ID."""
    return node_id.upper()


def _default_regex(alphabet: str, length: int) -> re.Pattern[str]:
    return re.compile(rf"^[{re.escape(alphabet)}]{{{length}}}$")


def generate_node_id(registry: Optional[NodeRegistry] = None) -> str:
    """Convenience: generate a single random node ID (no collision check)."""
    reg = registry or NodeRegistry()
    return reg._generate()


class NodeRegistry:
    """Maintains a set of active node IDs and generates collision-free new ones."""

    def __init__(self, alphabet: Optional[str] = None, length: int = NODE_ID_LENGTH) -> None:
        self.alphabet = alphabet or NODE_ID_ALPHABET
        self.length = length
        self._regex = _default_regex(self.alphabet, self.length)

    def _generate(self) -> str:
        return "".join(secrets.choice(self.alphabet) for _ in range(self.length))

    def is_valid_node_id(self, node_id: str) -> bool:
        """Return True if ``node_id`` matches the configured ADR-001 schema."""
        return bool(self._regex.match(node_id.upper()))

    def is_registered(self, node_id: str) -> bool:
        """Return True if the normalized node ID already exists in the DB."""
        normalized = normalize_node_id(node_id)
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM nodes WHERE node_id = ?", (normalized,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def generate_unique_node_id(
        self, exists_callback: Optional[callable] = None
    ) -> str:
        """Generate a node ID that does not exist yet.

        ``exists_callback`` receives a candidate ID and should return True if it
        is already taken.  Defaults to a DB lookup.
        """
        _exists = exists_callback or self.is_registered
        for _ in range(NODE_ID_MAX_RETRIES):
            candidate = self._generate()
            if not _exists(candidate):
                return candidate
        raise NodeIdExhaustedError(
            f"Could not allocate a unique node ID after {NODE_ID_MAX_RETRIES} attempts"
        )

    @staticmethod
    def _format_time(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat()

    def create_node(
        self,
        node_name: str,
        endpoint: Optional[str],
        capabilities: List[Dict[str, Any]],
        role: str,
        status: str,
        preferred_node_id: Optional[str] = None,
    ) -> str:
        """Create a node record and return the assigned node ID.

        If ``preferred_node_id`` is a valid ADR-001 ID that is not already
        registered, the registry will honour the request.  Otherwise it falls
        back to random generation.

        Returns the newly assigned node ID.  Raises :class:`NodeNameExistsError`
        if ``node_name`` is already taken.  Raises :class:`NodeIdExhaustedError`
        if no free ID can be allocated.
        """
        conn = get_conn()
        now = self._format_time(datetime.now(timezone.utc))
        assigned_node_id: Optional[str] = None

        try:
            for attempt in range(NODE_ID_MAX_RETRIES):
                if attempt == 0 and preferred_node_id is not None:
                    candidate = normalize_node_id(preferred_node_id)
                    if not self.is_valid_node_id(candidate):
                        candidate = self._generate()
                else:
                    candidate = self._generate()

                caps_json = json.dumps(capabilities) if capabilities else "[]"

                try:
                    conn.execute(
                        "INSERT INTO nodes ("
                        "node_id, node_name, endpoint, capabilities, "
                        "last_seen, registered_at, status, role"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (candidate, node_name, endpoint, caps_json, now, now, status, role),
                    )
                    conn.commit()
                    assigned_node_id = candidate
                    return candidate
                except sqlite3.IntegrityError as exc:
                    msg = str(exc).lower()
                    if "node_id" in msg:
                        continue
                    if "node_name" in msg:
                        raise NodeNameExistsError(
                            f"Node name already registered: {node_name}"
                        ) from exc
                    raise
            raise NodeIdExhaustedError(
                f"Could not allocate a unique node ID after {NODE_ID_MAX_RETRIES} attempts"
            )
        finally:
            conn.close()
            # T-026: keep the normalized capability index in sync for the
            # newly created node. Skipped if no candidate was assigned.
            if assigned_node_id is not None:
                try:
                    sync_node_capabilities(assigned_node_id, capabilities or [])
                except Exception:
                    pass

    def lookup(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Return node metadata or None.  Lookup is case-insensitive."""
        normalized = normalize_node_id(node_id)
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT node_id, node_name, endpoint, capabilities, "
                "status, role, last_seen, registered_at "
                "FROM nodes WHERE node_id = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                return None
            return {
                "node_id": row["node_id"],
                "node_name": row["node_name"],
                "endpoint": row["endpoint"],
                "capabilities": json.loads(row["capabilities"] or "[]"),
                "status": row["status"],
                "role": row["role"],
                "last_seen": row["last_seen"],
                "registered_at": row["registered_at"],
            }
        finally:
            conn.close()

    def list_nodes(
        self, status: Optional[str] = None, exclude_ids: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Return all nodes, optionally filtered by status or excluded IDs."""
        query = (
            "SELECT node_id, node_name, endpoint, capabilities, "
            "status, role, last_seen, registered_at FROM nodes"
        )
        params: List[Any] = []
        conditions: List[str] = []

        if status is not None:
            conditions.append("status = ?")
            params.append(status)

        if exclude_ids:
            placeholders = ", ".join("?" for _ in exclude_ids)
            conditions.append(f"node_id NOT IN ({placeholders})")
            params.extend(normalize_node_id(nid) for nid in exclude_ids)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        conn = get_conn()
        try:
            rows = conn.execute(query, params).fetchall()
            return [
                {
                    "node_id": row["node_id"],
                    "node_name": row["node_name"],
                    "endpoint": row["endpoint"],
                    "capabilities": json.loads(row["capabilities"] or "[]"),
                    "status": row["status"],
                    "role": row["role"],
                    "last_seen": row["last_seen"],
                    "registered_at": row["registered_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()
