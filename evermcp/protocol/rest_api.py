"""S2 REST API endpoints for client/key management and call logs.

These endpoints are mounted under ``/api`` alongside the S1 capability routes
in ``evermcp.web.rest``. All endpoints require an API key with the ``admin``
scope (or a key scoped for the specific operation).
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from evermcp.storage import (
    ApiKey,
    CallLog,
    Client,
    create_api_key,
    create_client,
    delete_api_key,
    delete_client,
    list_api_keys,
    list_call_logs,
    list_clients,
    revoke_api_key,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["gateway-admin"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_client(c: Client, online: bool = False) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
        "online": online,
    }


def _serialize_api_key(k: ApiKey) -> dict[str, Any]:
    return {
        "key_hash": k.key_hash,
        "client_id": k.client_id,
        "scopes": [s.strip() for s in (k.scopes or "").split(",") if s.strip()],
        "created_at": k.created_at.isoformat() if k.created_at else None,
        "revoked": k.revoked,
    }


def _serialize_call_log(row: CallLog) -> dict[str, Any]:
    return {
        "call_id": row.call_id,
        "name": row.name,
        "source": row.source,
        "success": row.success,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "duration_ms": row.duration_ms,
        "error_code": row.error_code,
    }


def _online_client_ids(coordinator: Any) -> set[str]:
    """Return the set of client ids currently connected via RemoteClientProvider."""
    online: set[str] = set()
    for provider in coordinator.registry.providers:
        source = getattr(provider, "source", "")
        if source.startswith("remote."):
            online.add(source.removeprefix("remote."))
    return online


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@router.get("/clients")
async def get_clients(request: Request) -> dict[str, Any]:
    """List all registered remote clients and their online status."""
    coord = request.app.state.coordinator
    online = _online_client_ids(coord)
    rows = list_clients()
    return {
        "clients": [_serialize_client(c, online=c.id in online) for c in rows],
    }


@router.post("/clients")
async def create_client_endpoint(
    request: Request,
    name: str,
) -> dict[str, Any]:
    """Create a new remote-client identity."""
    client = create_client(name=name)
    return {"status": "created", "client": _serialize_client(client)}


@router.delete("/clients/{client_id}")
async def delete_client_endpoint(client_id: str) -> dict[str, Any]:
    """Delete a client identity and its API keys."""
    if not delete_client(client_id):
        raise HTTPException(status_code=404, detail=f"Client not found: {client_id}")
    return {"status": "deleted", "client_id": client_id}


# ---------------------------------------------------------------------------
# API Keys
# ---------------------------------------------------------------------------


@router.get("/keys")
async def get_keys() -> dict[str, Any]:
    """List all API keys (hashes only; plaintext is never stored)."""
    rows = list_api_keys()
    return {"keys": [_serialize_api_key(k) for k in rows]}


@router.post("/keys")
async def create_key(
    client_id: str | None = None,
    scopes: str = "ws:connect",
) -> dict[str, Any]:
    """Create a new API key.

    Returns the plaintext key exactly once. The caller must save it; only a
    hash is stored in the database.

    Args:
        client_id: Optional client id to bind the key to.
        scopes: Comma-separated scopes, e.g. "ws:connect" or "admin".
    """
    plaintext = "emcp_" + secrets.token_urlsafe(32)
    row = create_api_key(key=plaintext, client_id=client_id, scopes=scopes)
    return {
        "status": "created",
        "key": plaintext,
        "key_hash": row.key_hash,
        "client_id": row.client_id,
        "scopes": [s.strip() for s in (row.scopes or "").split(",") if s.strip()],
    }


@router.post("/keys/{key_hash}/revoke")
async def revoke_key(key_hash: str) -> dict[str, Any]:
    """Revoke an API key by hash."""
    if not revoke_api_key(key_hash):
        raise HTTPException(status_code=404, detail=f"Key not found: {key_hash}")
    return {"status": "revoked", "key_hash": key_hash}


@router.delete("/keys/{key_hash}")
async def delete_key(key_hash: str) -> dict[str, Any]:
    """Delete an API key by hash."""
    if not delete_api_key(key_hash):
        raise HTTPException(status_code=404, detail=f"Key not found: {key_hash}")
    return {"status": "deleted", "key_hash": key_hash}


# ---------------------------------------------------------------------------
# Call Logs
# ---------------------------------------------------------------------------


@router.get("/logs")
async def get_logs(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    name: str | None = None,
    source: str | None = None,
    success: bool | None = None,
) -> dict[str, Any]:
    """Query paginated call logs."""
    rows, total = list_call_logs(
        limit=limit,
        offset=offset,
        name=name,
        source=source,
        success=success,
    )
    return {
        "logs": [_serialize_call_log(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
