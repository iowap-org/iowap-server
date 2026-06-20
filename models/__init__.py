"""Pydantic models for the AI-Relay-Service v2 API."""

from typing import List, Optional

from pydantic import BaseModel, Field


class Capability(BaseModel):
    name: str
    version: str = "1.0.0"
    consumes: Optional[List[str]] = None
    produces: Optional[List[str]] = None


class NodeRegistration(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    node_name: str = Field(..., min_length=1, max_length=256)
    endpoint: Optional[str] = None
    capabilities: List[Capability] = Field(default_factory=list)
    role: str = "worker"
    bootstrap_secret: Optional[str] = None


class NodeApproval(BaseModel):
    role: Optional[str] = None
    capabilities: Optional[List[Capability]] = None
    endpoint: Optional[str] = None


class TokenResponse(BaseModel):
    node_id: str
    node_name: str
    status: str
    token_type: str
    token: str
    expires_at: str


class AuthContext(BaseModel):
    token_id: str
    node_id: str
    node_name: str
    endpoint: Optional[str]
    capabilities: List[Capability]
    status: str
    role: str
    token_type: str
    pending: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin" and self.status == "approved" and not self.pending

    @property
    def is_approved(self) -> bool:
        return self.status == "approved" and not self.pending
