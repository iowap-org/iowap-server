"""
Discovery-Modelle – Anfrage und Antwort für Capability-Abfragen.

Capability-Namen: Kombination aus Service + Mode
  "image.gen"      → Full-Feature AI-Bildgenerierung
  "mflux-schnell"  → Schneller Script-Modus (Prompt rein, Bild raus)

Derselbe Capability-Name kann von mehreren Nodes angeboten werden.
Discovery fasst sie zusammen und liefert pro Capability alle Nodes.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class DiscoveryNode(BaseModel):
    """Ein Node, der eine bestimmte Capability anbietet."""

    node_id: str
    node_name: str
    available: bool = True
    load: float = 0.0
    queue_depth: int = 0
    last_seen: str  # ISO-Format
    config: dict[str, Any] = Field(default_factory=dict)


class DiscoveryCapability(BaseModel):
    """Eine Capability mit allen Nodes, die sie anbieten."""

    name: str
    type: str  # ai, tool, script, workflow, resource
    description: str = ""
    version: str = "1.0.0"
    available: bool = True
    input_schema: Optional[dict[str, Any]] = Field(
        None,
        description="Input-Schema für Task-Validierung (aus capabilities.yaml)",
    )
    nodes: list[DiscoveryNode] = Field(default_factory=list)


class DiscoveryResponse(BaseModel):
    """Antwort auf GET /discovery/capabilities."""

    capabilities: list[DiscoveryCapability]


class DiscoveryDetailResponse(BaseModel):
    """Antwort auf GET /discovery/capabilities/{name}."""

    name: str
    type: str
    description: str
    version: str
    available: bool
    input_schema: Optional[dict[str, Any]] = None
    nodes: list[DiscoveryNode]