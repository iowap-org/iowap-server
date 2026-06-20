"""Cluster-side node ID minting and uniqueness registry.

ADR-001: node IDs are 8-character strings from an unambiguous alphanumeric
alphabet.  The cluster mints them, never the client.
"""

from __future__ import annotations

import secrets
from typing import Optional

NODE_ID_ALPHABET = "ABCDEFGHJKMNPQRTUVWXY346789"
NODE_ID_LENGTH = 8
NODE_ID_MAX_RETRIES = 100


def _generate_random_node_id() -> str:
    """Return one random 8-character ADR-001 node ID."""
    return "".join(secrets.choice(NODE_ID_ALPHABET) for _ in range(NODE_ID_LENGTH))


class NodeRegistry:
    """Maintains a set of active node IDs and generates collision-free new ones."""

    def __init__(self, alphabet: Optional[str] = None, length: int = NODE_ID_LENGTH) -> None:
        self.alphabet = alphabet or NODE_ID_ALPHABET
        self.length = length

    def _generate(self) -> str:
        return "".join(secrets.choice(self.alphabet) for _ in range(self.length))

    def generate_unique_node_id(
        self, exists_callback: Optional[callable] = None
    ) -> str:
        """Generate a node ID that does not exist yet.

        ``exists_callback`` receives a candidate ID and should return True if it
        is already taken.  Defaults to always False (useful in tests).
        """
        for _ in range(NODE_ID_MAX_RETRIES):
            candidate = self._generate()
            if exists_callback is None or not exists_callback(candidate):
                return candidate
        raise RuntimeError("Could not generate a unique node ID")

    def is_valid_node_id(self, node_id: str) -> bool:
        if len(node_id) != self.length:
            return False
        if not all(ch in self.alphabet for ch in node_id):
            return False
        return True


def generate_node_id(registry: Optional[NodeRegistry] = None) -> str:
    """Convenience: generate a single random node ID (no collision check)."""
    reg = registry or NodeRegistry()
    return reg._generate()
