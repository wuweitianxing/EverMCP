"""SQLite persistence for EverMCP gateway (S0 base).

Tables (S0 ships all three so S1/S2 don't have to migrate):
    InlineCapability  — UI form-declared capabilities (S1 reads, S1 writes via REST)
    Client            — Remote-client identity (S2 reads/writes via WS auth)
    ApiKey            — Hashed API keys (S2 auth; only hash stored, not plaintext)
    CallLog           — Tool call audit log (S2 writes)

S0 only writes the schema (init_db). No CRUD methods yet — those land with
the consumers. Read accessors are limited to `list_inline_capabilities()`
which the future InlineDeclarationProvider will call.
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Field, Session, SQLModel, select


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DB_URL = "sqlite:///~/.evermcp/evermcp.db"


# ---------------------------------------------------------------------------
# Table models
# ---------------------------------------------------------------------------


class InlineCapability(SQLModel, table=True):
    """A capability declared via the gateway UI form (S1).

    `kind` is one of "tool" / "resource" / "prompt".
    `schema_json` holds a JSON-encoded input_schema / arguments / uri_template
    depending on `kind` — kept as a string for S0 simplicity; promote to
    a typed column or JSON column in a later milestone if querying into it
    becomes useful.
    """

    __tablename__ = "inline_capability"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    kind: str
    name: str
    source: str = "inline"
    description: str = ""
    schema_json: str = "{}"
    enabled: bool = True
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Client(SQLModel, table=True):
    """Remote-client identity, populated by WS auth (S2)."""

    __tablename__ = "client"

    id: str = Field(default_factory=lambda: uuid4().hex, primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime | None = None


class ApiKey(SQLModel, table=True):
    """Hashed API key. Plaintext is never stored."""

    __tablename__ = "api_key"

    key_hash: str = Field(primary_key=True)
    client_id: str | None = None
    scopes: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revoked: bool = False


class CallLog(SQLModel, table=True):
    """Audit log entry for a tool call (S2 writes)."""

    __tablename__ = "call_log"

    call_id: str = Field(primary_key=True)
    name: str
    source: str = "local"
    success: bool
    started_at: datetime
    duration_ms: int = 0
    error_code: int | None = None


# ---------------------------------------------------------------------------
# Engine + init
# ---------------------------------------------------------------------------


def _ensure_sqlite_dir(db_url: str) -> None:
    """Create the parent directory for a sqlite URL if it points at a file.

    SQLite's URL form is sqlite:///absolute/or/~/path.db — strip the
    scheme prefix, expand the user, and mkdir -p the parent.
    """
    if not db_url.startswith("sqlite:"):
        return
    # Strip scheme: sqlite:///foo/bar.db -> /foo/bar.db, sqlite:///~/foo -> ~/foo
    path_part = db_url.split("sqlite:", 1)[1]
    # Drop the host segment ("///foo" -> "/foo", "//foo" -> "/foo")
    # but keep "~" intact for expanduser.
    if path_part.startswith("///"):
        raw = path_part[3:]
    elif path_part.startswith("//"):
        raw = "/" + path_part[2:]
    else:
        raw = path_part
    if not raw:
        return
    db_path = Path(os.path.expanduser(raw))
    if db_path.parent and str(db_path.parent) not in ("", "."):
        db_path.parent.mkdir(parents=True, exist_ok=True)


def _normalize_sqlite_url(db_url: str) -> str:
    """Clean implementation: expand `~` and convert path to POSIX form."""
    if not db_url.startswith("sqlite:"):
        return db_url
    suffix = db_url[len("sqlite:"):]
    # suffix is like "///~/foo/bar.db" or "///C:/foo/bar.db" or ":memory:"
    if suffix.startswith("///"):
        body = suffix[3:]
    elif suffix.startswith("//"):
        body = "/" + suffix[2:]
    else:
        body = suffix
    # In-memory / special URLs (no path component).
    if not body or body == ":memory:":
        return db_url
    expanded = os.path.expanduser(body)
    posix = Path(expanded).as_posix()
    return f"sqlite:///{posix}"


def _enable_sqlite_fk_on_connect(
    dbapi_connection: Any, _connection_record: Any
) -> None:
    """Enable FK enforcement for a single sqlite connection.

    Attached per-engine below — not at module import. Attaching to
    ``sqlalchemy.engine.Engine`` (the base class) at import time would
    silently hook every Engine in the host process, including ones the
    caller owns.
    """
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    except Exception:
        # Non-sqlite dialect (shouldn't happen — we only listen for sqlite
        # engines, but defensive). Silently ignore.
        pass


def get_engine(db_url: str | None = None) -> Engine:
    """Return a SQLAlchemy Engine for `db_url` (default: SQLite under ~/.evermcp/).

    Lazy: only constructs the engine (and creates the parent directory for
    sqlite file URLs) when called. Safe to call repeatedly — SQLAlchemy
    caches the engine internally per URL.

    For sqlite engines, also attaches a per-engine ``connect`` listener that
    turns on FK enforcement. The listener is scoped to the engine we just
    created, not the global ``Engine`` base class — importing this module
    has no side effects on the host process's SQLAlchemy registry.
    """
    url = db_url if db_url is not None else DEFAULT_DB_URL
    url = _normalize_sqlite_url(url)
    _ensure_sqlite_dir(url)
    engine = _make_engine(url)
    if url.startswith("sqlite:"):
        event.listen(engine, "connect", _enable_sqlite_fk_on_connect)
    return engine


def _make_engine(url: str) -> Engine:
    """Fallback engine factory — SQLModel exposes `create_engine`."""
    from sqlmodel import create_engine

    return create_engine(url)


def init_db(engine: Engine | None = None) -> Engine:
    """Create all tables on the given engine (default: `get_engine()`).

    Idempotent — `SQLModel.metadata.create_all` is a no-op for existing
    tables. Returns the engine used so callers can chain.
    """
    eng = engine if engine is not None else get_engine()
    SQLModel.metadata.create_all(eng)
    return eng


# ---------------------------------------------------------------------------
# Accessors (S0 — minimal)
# ---------------------------------------------------------------------------


def list_inline_capabilities(engine: Engine | None = None) -> list[InlineCapability]:
    """Return all enabled InlineCapability rows.

    `engine=None` defers to `get_engine()`. Filters `enabled=True` so
    disabled capabilities don't surface to providers.
    """
    eng = engine if engine is not None else get_engine()
    with Session(eng) as session:
        statement = select(InlineCapability).where(InlineCapability.enabled == True)  # noqa: E712
        return list(session.exec(statement).all())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hash_api_key(key: str) -> str:
    """Return the sha256 hex digest of `key` — used as the storage key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()