"""API key authentication for EverMCP gateway (S2).

Supports:
- WebSocket handshake: ``?token=<api_key>`` query parameter or
  ``X-EverMCP-Key`` header.
- HTTP endpoints: ``Authorization: Bearer <api_key>`` header or
  ``X-EverMCP-Key`` header.

Only the sha256 hash of each key is stored. Plaintext keys are never
persisted.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Header, HTTPException, Query, WebSocketException, status

from evermcp.storage import ApiKey, get_api_key_by_plaintext

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when an API key is missing, unknown, revoked, or scoped incorrectly."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def validate_api_key(
    key: str | None,
    required_scope: str | None = "ws:connect",
    engine: Any = None,
) -> ApiKey:
    """Validate a plaintext API key.

    Args:
        key: The plaintext API key.
        required_scope: If not None, the key must have this scope (or "admin").
        engine: SQLAlchemy engine; defaults to ``get_engine()``.

    Returns:
        The validated ``ApiKey`` row.

    Raises:
        AuthError: if the key is missing, unknown, revoked, or lacks scope.
    """
    if not key:
        raise AuthError("Missing API key", status.HTTP_401_UNAUTHORIZED)

    if engine is None:
        # Local import so tests can monkeypatch ``evermcp.storage.get_engine``
        # without fighting module-level import caching.
        from evermcp.storage import get_engine

        engine = get_engine()
    row = get_api_key_by_plaintext(key, engine=engine)
    if row is None:
        raise AuthError("Invalid API key", status.HTTP_401_UNAUTHORIZED)
    if row.revoked:
        raise AuthError("Revoked API key", status.HTTP_401_UNAUTHORIZED)

    if required_scope:
        scopes = {s.strip() for s in (row.scopes or "").split(",") if s.strip()}
        if "admin" not in scopes and required_scope not in scopes:
            raise AuthError(
                f"API key lacks required scope: {required_scope}",
                status.HTTP_403_FORBIDDEN,
            )

    return row


def api_key_has_scope(api_key: ApiKey, scope: str) -> bool:
    """Return True if the API key has ``scope`` or the wildcard ``admin``."""
    scopes = {s.strip() for s in (api_key.scopes or "").split(",") if s.strip()}
    return "admin" in scopes or scope in scopes


async def require_api_key_http(
    authorization: str | None = Header(None, alias="Authorization"),
    x_key: str | None = Header(None, alias="X-EverMCP-Key"),
    required_scope: str = "admin",
) -> ApiKey:
    """FastAPI dependency: validate an API key from HTTP headers.

    Prefers ``Authorization: Bearer <key>``, falls back to ``X-EverMCP-Key``.
    Raises ``HTTPException(401/403)`` on failure.
    """
    key: str | None = None
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            key = parts[1].strip()
        elif not x_key:
            key = authorization.strip()
    if not key:
        key = x_key

    try:
        return validate_api_key(key, required_scope=required_scope)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


async def require_api_key_ws(
    token: str | None = Query(None, alias="token"),
    x_key: str | None = Header(None, alias="X-EverMCP-Key"),
    required_scope: str = "ws:connect",
) -> ApiKey:
    """FastAPI dependency: validate an API key from WebSocket handshake.

    Prefers the ``?token=<key>`` query parameter, falls back to the
    ``X-EverMCP-Key`` header.

    Raises:
        WebSocketException: on failure (code 1008 = policy violation).
    """
    key = token or x_key
    try:
        return validate_api_key(key, required_scope=required_scope)
    except AuthError as exc:
        logger.warning("WS auth failed: %s", exc.message)
        raise WebSocketException(code=1008, reason=exc.message) from exc


__all__ = [
    "AuthError",
    "api_key_has_scope",
    "require_api_key_http",
    "require_api_key_ws",
    "validate_api_key",
]
