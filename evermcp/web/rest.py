"""REST API endpoints for EverMCP gateway UI (S1).

Endpoints:
- GET  /api/tree          — Capability node tree grouped by source/category
- GET  /api/capabilities  — Flat list of all capabilities
- POST /api/capabilities  — Create an inline capability
- PUT  /api/capabilities  — Update an inline capability
- DELETE /api/capabilities — Delete an inline capability
- POST /api/test          — Test call a capability
- GET  /api/health        — Provider health status
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from mcp.shared.exceptions import McpError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["gateway"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_coordinator(request: Request) -> Any:
    """Extract the Coordinator from the FastAPI app state."""
    coord = request.app.state.coordinator
    if coord is None:
        raise HTTPException(status_code=500, detail="Coordinator not configured")
    return coord


def _get_inline_provider(coord: Any) -> Any:
    """Extract the InlineDeclarationProvider from the coordinator's registry."""
    registry = coord.registry
    if registry is None:
        raise HTTPException(status_code=500, detail="Registry not configured")

    # Find the inline provider among the registry's providers.
    # Use the public `providers` property (returns a list copy) instead of
    # reaching into the private `_providers` attribute.
    for provider in registry.providers:
        if provider.source == "inline":
            return provider
    return None


# ---------------------------------------------------------------------------
# Capability tree
# ---------------------------------------------------------------------------


@router.get("/tree")
async def get_tree(request: Request) -> dict[str, Any]:
    """Return the capability node tree grouped by source and category.

    Response format:
    {
      "groups": [
        {
          "source": "local",
          "label": "本地文件",
          "children": [
            {
              "category": "io",
              "children": [
                {
                  "name": "local.io.read_file",
                  "kind": "tool",
                  "enabled": true,
                  "health": "healthy"
                }
              ]
            }
          ]
        }
      ]
    }
    """
    coord = _get_coordinator(request)

    groups: dict[str, dict[str, Any]] = {}

    for provider in coord.registry.providers:
        source = provider.source
        if source not in groups:
            label = {
                "local": "本地文件",
                "inline": "内联声明",
            }.get(source, source)
            if source.startswith("remote."):
                client_id = source.removeprefix("remote.")
                label = f"远程: {client_id}"
            groups[source] = {"source": source, "label": label, "children": {}}

        for cap in provider.list_capabilities():
            # Extract category from name (format: "category.tool_name" for local)
            parts = cap.name.split(".")
            if source == "local" and len(parts) >= 2:
                category = parts[0]
                full_name = f"local.{cap.name}"
            elif source == "inline":
                category = "inline"
                full_name = f"inline.{cap.name}"
            elif source.startswith("remote."):
                category = "tools"
                full_name = cap.name
            else:
                category = "other"
                full_name = cap.name

            if category not in groups[source]["children"]:
                groups[source]["children"][category] = {"category": category, "children": []}

            groups[source]["children"][category]["children"].append(
                {
                    "name": full_name,
                    "kind": cap.kind.value,
                    "enabled": cap.enabled,
                    "health": "healthy" if provider.health() else "offline",
                    "description": cap.description,
                }
            )

    # Convert children dict to sorted list
    result_groups = []
    for source_data in groups.values():
        children_list = sorted(
            source_data["children"].values(), key=lambda x: x.get("category", "")
        )
        source_data["children"] = children_list
        result_groups.append(source_data)

    return {"groups": result_groups}


@router.get("/capabilities")
async def list_capabilities(request: Request) -> dict[str, Any]:
    """Return a flat list of all capabilities."""
    coord = _get_coordinator(request)

    caps = []
    for provider in coord.registry.providers:
        for cap in provider.list_capabilities():
            parts = cap.name.split(".")
            if provider.source == "local" and len(parts) >= 2:
                full_name = f"local.{cap.name}"
            elif provider.source == "inline":
                full_name = f"inline.{cap.name}"
            elif provider.source.startswith("remote."):
                full_name = cap.name
            else:
                full_name = cap.name

            caps.append(
                {
                    "name": full_name,
                    "kind": cap.kind.value,
                    "source": provider.source,
                    "enabled": cap.enabled,
                    "health": "healthy" if provider.health() else "offline",
                    "description": cap.description,
                }
            )

    return {"capabilities": caps}


# ---------------------------------------------------------------------------
# Capability CRUD (inline only)
# ---------------------------------------------------------------------------


@router.post("/capabilities")
async def create_capability(request: Request) -> dict[str, Any]:
    """Create a new inline capability."""
    body = await request.json()

    kind = body.get("kind")
    name = body.get("name")
    description = body.get("description", "")
    schema_json = body.get("schema_json", "{}")
    enabled = body.get("enabled", True)

    if not kind or kind not in ("tool", "resource", "prompt"):
        raise HTTPException(status_code=400, detail="Invalid or missing 'kind'")
    if not name:
        raise HTTPException(status_code=400, detail="Missing 'name'")

    provider = _get_inline_provider(_get_coordinator(request))
    if provider is None:
        raise HTTPException(status_code=500, detail="InlineDeclarationProvider not found")

    provider.add_capability(
        kind=kind,
        name=name,
        description=description,
        schema_json=schema_json,
        enabled=enabled,
    )

    return {"status": "created", "name": name}


@router.put("/capabilities")
async def update_capability(request: Request) -> dict[str, Any]:
    """Update an existing inline capability.

    Supports selective updates to ``enabled``, ``description`` and
    ``schema_json`` — only fields present in the request body are changed.

    The InlineDeclarationProvider only exposes ``update_capability_enabled``,
    so to support description / schema_json edits we write the row directly
    (filtered by name + source="inline") and then call ``provider._reload()``
    to refresh the in-memory cache. This keeps provider.py untouched while
    giving the UI full edit capability.
    """
    from datetime import UTC, datetime

    from sqlalchemy import select

    from evermcp.storage import InlineCapability, Session

    body = await request.json()

    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Missing 'name'")

    provider = _get_inline_provider(_get_coordinator(request))
    if provider is None:
        raise HTTPException(status_code=500, detail="InlineDeclarationProvider not found")

    # The UI sends names prefixed with "inline." (see /api/tree); strip it to
    # get the bare capability name stored in the DB.
    base_name = name.replace("inline.", "", 1)

    # Selective update — only fields present in the body are changed.
    updates: dict[str, Any] = {}
    if "enabled" in body:
        updates["enabled"] = body["enabled"]
    if "description" in body:
        updates["description"] = body["description"]
    if "schema_json" in body:
        updates["schema_json"] = body["schema_json"]

    if not updates:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    engine = getattr(provider, "_engine", None)
    if engine is None:
        raise HTTPException(status_code=500, detail="Provider engine not available")

    with Session(engine) as session:
        stmt = select(InlineCapability).where(
            InlineCapability.name == base_name,
            InlineCapability.source == "inline",
        )
        instance = session.scalars(stmt).first()
        if instance is None:
            raise HTTPException(status_code=404, detail=f"Capability not found: {name}")

        if "enabled" in updates:
            instance.enabled = updates["enabled"]
        if "description" in updates:
            instance.description = updates["description"]
        if "schema_json" in updates:
            instance.schema_json = updates["schema_json"]
        instance.updated_at = datetime.now(UTC)
        session.commit()

    # Refresh the in-memory cache so subsequent reads reflect the change.
    provider._reload()

    return {"status": "updated", "name": name}


@router.delete("/capabilities")
async def delete_capability(request: Request) -> dict[str, Any]:
    """Delete an inline capability."""
    body = await request.json()

    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="Missing 'name'")

    provider = _get_inline_provider(_get_coordinator(request))
    if provider is None:
        raise HTTPException(status_code=500, detail="InlineDeclarationProvider not found")

    base_name = name.replace("inline.", "", 1)

    if not provider.delete_capability(base_name):
        raise HTTPException(status_code=404, detail=f"Capability not found: {name}")

    return {"status": "deleted", "name": name}


# ---------------------------------------------------------------------------
# Test call
# ---------------------------------------------------------------------------


@router.post("/test")
async def test_call(request: Request) -> dict[str, Any]:
    """Test calling a capability."""
    body = await request.json()

    raw_name = body.get("name", "")
    args = body.get("args", {})

    if not raw_name:
        raise HTTPException(status_code=400, detail="Missing 'name'")

    # /api/tree prefixes capability names with "local." / "inline." to
    # disambiguate sources in the UI, but the registry routes by the bare
    # capability name. Strip the prefix before dispatching.
    base_name = (
        raw_name.split(".", 1)[1] if raw_name.startswith(("local.", "inline.")) else raw_name
    )

    coord = _get_coordinator(request)

    try:
        if body.get("kind") == "tool":
            result = await coord.call_tool_async(base_name, args)
            return {"success": True, "result": result}
        elif body.get("kind") == "resource":
            # Resources are routed by URI, not by name. Reverse-lookup the
            # resource descriptor whose name matches base_name to recover its
            # URI, then read it. read_resource returns a (content, mime_type)
            # tuple which is unpacked into a JSON-safe dict (decode bytes so
            # FastAPI can serialize the response).
            uri = None
            for desc in coord.registry.list_resources():
                if desc.get("name") == base_name:
                    uri = desc.get("uri")
                    break
            if uri is None:
                return {"success": False, "error": f"Resource not found: {raw_name}"}
            content, mime_type = await coord.read_resource(uri)
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")
            return {"success": True, "result": {"content": content, "mimeType": mime_type}}
        elif body.get("kind") == "prompt":
            result = await coord.get_prompt(base_name, args)
            return {"success": True, "result": result}
        else:
            raise HTTPException(status_code=400, detail="Missing or invalid 'kind'")
    except McpError as exc:
        return {"success": False, "error": str(exc)}
    except Exception as exc:
        logger.exception("Test call failed for %s", raw_name)
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Register routes
# ---------------------------------------------------------------------------


def register_routes(app: Any) -> None:
    """Register all REST API routes on the FastAPI app."""
    app.include_router(router)
