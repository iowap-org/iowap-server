"""
Discovery models – request and response for capability queries.

Capability names: combination of service + mode
  "image.gen"      → full-feature AI image generation
  "mflux-schnell"  → fast script mode (prompt in, image out)

The same capability name can be offered by multiple nodes.
Discovery aggregates them and returns all nodes per capability.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class DiscoveryNode(BaseModel):
    """A node that offers a specific capability."""

    node_id: str
    node_name: str
    available: bool = True
    load: float = 0.0
    queue_depth: int = 0
    last_seen: str  # ISO format
    config: dict[str, Any] = Field(default_factory=dict)


class DiscoveryCapability(BaseModel):
    """A capability with all nodes that offer it."""

    name: str
    type: Optional[str] = None
    description: str = ""
    version: str = "1.0.0"
    available: bool = True
    input_schema: Optional[dict[str, Any]] = Field(
        None,
        description="Input schema for task validation (from capabilities.yaml)",
    )
    nodes: list[DiscoveryNode] = Field(default_factory=list)


class DiscoveryResponse(BaseModel):
    """Response for GET /discovery/capabilities."""

    capabilities: list[DiscoveryCapability]


class DiscoveryDetailResponse(BaseModel):
    """Response for GET /discovery/capabilities/{name}."""

    name: str
    type: str
    description: str
    version: str
    available: bool
    input_schema: Optional[dict[str, Any]] = None
    nodes: list[DiscoveryNode]