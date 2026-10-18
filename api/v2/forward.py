"""Forward-mapping management router (T-Forward).

Admin-only CRUD for (submitter node, slot) → target capability mappings,
mounted at ``/relay/v2/forward-mappings`` (api/v2/__init__.py). The submit
side consumes the same rows through ``core/forward.py``. Auth: admin role
via ``require_admin_or_dashboard_user`` + ``ctx.is_admin`` (admin node
token, human admin session or master). Contracts:
``.hermes/pipeline/t-forward/design.md`` §3.4.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from relay_server.api.v2.security import require_admin_or_dashboard_user
from relay_server.core.db import log_audit_event
from relay_server.core.forward import (
    ForwardRoutingError,
    delete_forward_mapping,
    list_forward_mappings,
    list_forward_slots,
    normalize_slot,
    upsert_forward_mapping,
)
from relay_server.models import (
    AuthContext,
    ForwardMappingEntry,
    ForwardMappingListResponse,
    ForwardMappingPut,
)

router = APIRouter()


def _require_forward_admin(ctx: AuthContext) -> None:
    """Allow the master user, admin human users, and admin-role node tokens only."""
    if not ctx.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")


def _to_http(exc: ForwardRoutingError) -> HTTPException:
    """Uniform <code>: <message> error mapping (design.md §3.4)."""
    return HTTPException(status_code=exc.http_status, detail=f"{exc.code}: {exc.message}")


@router.get("")
async def list_forward_mappings_endpoint(
    node: Optional[str] = Query(None, description="Optional submitter node id filter"),  # noqa: UP045  (frozen signature)
    ctx: AuthContext = Depends(require_admin_or_dashboard_user),  # noqa: B008  (api/v2 idiom)
) -> ForwardMappingListResponse:
    """List forward mappings (all, or filtered by ``?node=``)."""
    _require_forward_admin(ctx)
    try:
        rows = list_forward_mappings(node)
    except ForwardRoutingError as exc:
        raise _to_http(exc) from exc
    return ForwardMappingListResponse(mappings=[ForwardMappingEntry(**m) for m in rows])


@router.get("/slots")
async def list_forward_slots_endpoint(
    node: Optional[str] = Query(None, description="Optional submitter node id filter"),  # noqa: UP045  (frozen signature)
    ctx: AuthContext = Depends(require_admin_or_dashboard_user),  # noqa: B008  (api/v2 idiom)
) -> dict[str, Any]:
    """List node-declared forward slots (T-Forward-2) — metadata from node.yaml."""
    _require_forward_admin(ctx)
    try:
        rows = list_forward_slots(node)
    except ForwardRoutingError as exc:
        raise _to_http(exc) from exc
    return {"slots": rows}


@router.put("/{node_id}/{slot}")
async def upsert_forward_mapping_endpoint(
    node_id: str,
    slot: str,
    body: ForwardMappingPut,
    ctx: AuthContext = Depends(require_admin_or_dashboard_user),  # noqa: B008  (api/v2 idiom)
) -> ForwardMappingEntry:
    """Create or update one mapping. Body: ``{"target_capability": "…"}``."""
    _require_forward_admin(ctx)
    try:
        normalized = normalize_slot(slot)
        row = upsert_forward_mapping(node_id, slot, body.target_capability)
    except ForwardRoutingError as exc:
        raise _to_http(exc) from exc
    log_audit_event(
        actor_id=ctx.node_id,
        actor_name=ctx.node_name,
        action="forward.mapping_upsert",
        resource_type="node",
        resource_id=node_id,
        details=f"slot={normalized} target={body.target_capability}",
    )
    return ForwardMappingEntry(**row)


@router.delete("/{node_id}/{slot}")
async def delete_forward_mapping_endpoint(
    node_id: str,
    slot: str,
    ctx: AuthContext = Depends(require_admin_or_dashboard_user),  # noqa: B008  (api/v2 idiom)
) -> dict[str, Any]:
    """Delete one mapping; 404 ``forward_mapping_missing`` when absent."""
    _require_forward_admin(ctx)
    try:
        normalized = normalize_slot(slot)
        delete_forward_mapping(node_id, slot)
    except ForwardRoutingError as exc:
        raise _to_http(exc) from exc
    log_audit_event(
        actor_id=ctx.node_id,
        actor_name=ctx.node_name,
        action="forward.mapping_delete",
        resource_type="node",
        resource_id=node_id,
        details=f"slot={normalized}",
    )
    return {"deleted": True, "node_id": node_id, "slot": normalized}