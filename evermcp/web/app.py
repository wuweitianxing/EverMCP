"""FastAPI web application for EverMCP gateway UI (S1).

Provides:
- Local token authentication (first-run random token, stored in browser cookie)
- Static file serving for the Vue3 frontend (CDN-based, zero build)
- REST API endpoints for capability tree, CRUD, and test calls
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from evermcp.protocol.coordinator import Coordinator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Local token management
# ---------------------------------------------------------------------------

_TOKEN_FILE = Path(os.path.expanduser("~/.evermcp/token"))


def _get_or_create_token() -> str:
    """Return the local token, creating one if it doesn't exist."""
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text().strip()
    import secrets

    token = secrets.token_urlsafe(32)
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(token)
    logger.info("Generated new local token for UI auth")
    return token


# ---------------------------------------------------------------------------
# Token authentication middleware
# ---------------------------------------------------------------------------


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Validate bearer-token auth on ``/api/*`` routes.

    Public pass-through: ``/``, ``/health``, ``/static/*``. All non-API
    paths are also passed through. Only ``/api/*`` is gated, reading the
    token from the ``Authorization: Bearer <token>`` header or the
    ``evermcp_token`` cookie.
    """

    def __init__(self, app: Any, token: str) -> None:
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        path = request.url.path
        # Pass through health check, frontend entry, and static assets.
        if path in ("/", "/health") or path.startswith("/static"):
            return await call_next(request)
        # Only /api/* is token-gated.
        if not path.startswith("/api/"):
            return await call_next(request)
        # Read token: prefer Authorization header, fall back to cookie.
        auth = request.headers.get("authorization", "")
        token = ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = request.cookies.get("evermcp_token", "")
        if token != self._token:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing token"},
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(coordinator: Coordinator, require_token: bool = False) -> FastAPI:
    """Create the FastAPI application wired to the given coordinator.

    Args:
        coordinator: The MCP Coordinator instance for capability routing.
        require_token: When True, gate ``/api/*`` behind the local bearer
            token (UI/production mode). Defaults to False so existing
            tests using ``create_app(coordinator)`` are unaffected.

    Returns:
        A configured FastAPI app instance.
    """
    app = FastAPI(
        title="EverMCP Gateway",
        description="MCP Capability Governance UI",
        version="0.3.0",
    )

    # CORS — allow localhost/loopback origins across any port. Starlette's
    # CORSMiddleware does not support wildcard port strings in
    # ``allow_origins`` (they are treated as literals), so use a regex.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Token auth (inner layer). Added after CORS so CORS stays outermost
    # for preflight handling. Only enabled in UI/production mode.
    if require_token:
        app.add_middleware(TokenAuthMiddleware, token=_get_or_create_token())

    # Attach coordinator for API handlers
    app.state.coordinator = coordinator

    # Import and register REST API routes
    from evermcp.web.rest import register_routes

    register_routes(app)

    # Serve static frontend files
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "version": "0.3.0"}

    # ------------------------------------------------------------------
    # Frontend entry point
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        """Serve the main UI page.

        In token-protected mode, inject the local token into the page so
        the frontend's ``apiHeaders()`` helper can read
        ``window.EVERMCP_TOKEN`` and attach it to every fetch call.
        """
        html = (Path(__file__).parent / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        if require_token:
            token = _get_or_create_token()
            inject = f'<script>window.EVERMCP_TOKEN = "{token}";</script>'
            html = html.replace("</head>", inject + "</head>", 1)
        return HTMLResponse(html)

    return app
