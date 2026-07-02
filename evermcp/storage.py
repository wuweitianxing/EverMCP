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
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, desc, event, func
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
    name: str = Field(index=True)
    source: str = Field(default="local", index=True)
    success: bool
    started_at: datetime = Field(index=True)
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
    suffix = db_url[len("sqlite:") :]
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


def _enable_sqlite_fk_on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
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
# Client CRUD (S2)
# ---------------------------------------------------------------------------


def create_client(
    name: str,
    client_id: str | None = None,
    engine: Engine | None = None,
) -> Client:
    """Create a new remote-client identity.

    If `client_id` is not provided, a random hex string is generated.
    Returns the persisted Client row.
    """
    eng = engine if engine is not None else get_engine()
    client = Client(
        id=client_id or uuid4().hex,
        name=name,
        created_at=datetime.now(UTC),
        last_seen_at=None,
    )
    with Session(eng) as session:
        session.add(client)
        session.commit()
        session.refresh(client)
    return client


def get_client(client_id: str, engine: Engine | None = None) -> Client | None:
    """Return a Client by id, or None if not found."""
    eng = engine if engine is not None else get_engine()
    with Session(eng) as session:
        return session.get(Client, client_id)


def list_clients(engine: Engine | None = None) -> list[Client]:
    """Return all clients ordered by creation time."""
    eng = engine if engine is not None else get_engine()
    with Session(eng) as session:
        statement = select(Client).order_by(desc(Client.created_at))  # type: ignore[arg-type]
        return list(session.exec(statement).all())


def update_client_last_seen(client_id: str, engine: Engine | None = None) -> bool:
    """Update ``last_seen_at`` to now. Returns True if the client exists."""
    eng = engine if engine is not None else get_engine()
    with Session(eng) as session:
        client = session.get(Client, client_id)
        if client is None:
            return False
        client.last_seen_at = datetime.now(UTC)
        session.add(client)
        session.commit()
    return True


def delete_client(client_id: str, engine: Engine | None = None) -> bool:
    """Delete a client and any associated API keys. Returns True if found."""
    eng = engine if engine is not None else get_engine()
    with Session(eng) as session:
        client = session.get(Client, client_id)
        if client is None:
            return False
        # Cascade manually: delete API keys bound to this client.
        keys = session.exec(select(ApiKey).where(ApiKey.client_id == client_id)).all()
        for key in keys:
            session.delete(key)
        session.delete(client)
        session.commit()
    return True


# ---------------------------------------------------------------------------
# ApiKey CRUD (S2)
# ---------------------------------------------------------------------------


def create_api_key(
    key: str,
    client_id: str | None = None,
    scopes: str = "ws:connect",
    engine: Engine | None = None,
) -> ApiKey:
    """Store a hashed API key.

    ``key`` is the plaintext key; only its sha256 hash is persisted.
    ``scopes`` is a comma-separated string, e.g. "ws:connect,admin".
    """
    eng = engine if engine is not None else get_engine()
    row = ApiKey(
        key_hash=hash_api_key(key),
        client_id=client_id,
        scopes=scopes,
        created_at=datetime.now(UTC),
        revoked=False,
    )
    with Session(eng) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def get_api_key_by_hash(
    key_hash: str,
    engine: Engine | None = None,
) -> ApiKey | None:
    """Return an API key row by its hash, or None."""
    eng = engine if engine is not None else get_engine()
    with Session(eng) as session:
        return session.get(ApiKey, key_hash)


def get_api_key_by_plaintext(
    key: str,
    engine: Engine | None = None,
) -> ApiKey | None:
    """Return an API key row by plaintext, or None."""
    return get_api_key_by_hash(hash_api_key(key), engine=engine)


def list_api_keys(engine: Engine | None = None) -> list[ApiKey]:
    """Return all API keys ordered by creation time."""
    eng = engine if engine is not None else get_engine()
    with Session(eng) as session:
        statement = select(ApiKey).order_by(desc(ApiKey.created_at))  # type: ignore[arg-type]
        return list(session.exec(statement).all())


def revoke_api_key(key_hash: str, engine: Engine | None = None) -> bool:
    """Revoke an API key by hash. Returns True if found."""
    eng = engine if engine is not None else get_engine()
    with Session(eng) as session:
        row = session.get(ApiKey, key_hash)
        if row is None:
            return False
        row.revoked = True
        session.add(row)
        session.commit()
    return True


def delete_api_key(key_hash: str, engine: Engine | None = None) -> bool:
    """Delete an API key by hash. Returns True if found."""
    eng = engine if engine is not None else get_engine()
    with Session(eng) as session:
        row = session.get(ApiKey, key_hash)
        if row is None:
            return False
        session.delete(row)
        session.commit()
    return True


# ---------------------------------------------------------------------------
# CallLog CRUD (S2)
# ---------------------------------------------------------------------------


def create_call_log(
    call_id: str,
    name: str,
    source: str,
    success: bool,
    started_at: datetime,
    duration_ms: int = 0,
    error_code: int | None = None,
    engine: Engine | None = None,
) -> CallLog:
    """Persist a call-log entry."""
    eng = engine if engine is not None else get_engine()
    row = CallLog(
        call_id=call_id,
        name=name,
        source=source,
        success=success,
        started_at=started_at,
        duration_ms=duration_ms,
        error_code=error_code,
    )
    with Session(eng) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def list_call_logs(
    limit: int = 100,
    offset: int = 0,
    name: str | None = None,
    source: str | None = None,
    success: bool | None = None,
    engine: Engine | None = None,
) -> tuple[list[CallLog], int]:
    """Return paginated call logs plus total count.

    Filters are applied as AND conditions. ``limit`` is capped at 1000.
    """
    eng = engine if engine is not None else get_engine()
    limit = min(max(limit, 1), 1000)
    offset = max(offset, 0)

    with Session(eng) as session:
        filters = []
        if name:
            filters.append(CallLog.name == name)
        if source:
            filters.append(CallLog.source == source)
        if success is not None:
            filters.append(CallLog.success == success)

        statement = select(CallLog)
        count_statement = select(func.count()).select_from(CallLog)
        if filters:
            for f in filters:
                statement = statement.where(f)
                count_statement = count_statement.where(f)

        total: int = session.scalar(count_statement) or 0
        rows = list(
            session.exec(
                statement.order_by(desc(CallLog.started_at))  # type: ignore[arg-type]
                .offset(offset)
                .limit(limit)
            ).all()
        )
        return rows, total


def prune_call_logs(keep: int = 10000, engine: Engine | None = None) -> int:
    """Delete the oldest call logs beyond the most recent ``keep`` entries.

    Simple retention guard for the otherwise-unbounded audit log. Keeps the
    newest ``keep`` rows (by ``started_at`` descending) and deletes the rest.
    Returns the number of rows deleted, computed as ``total - keep`` so the
    value is reliable even where SQLite ``rowcount`` is not.
    """
    keep = max(keep, 0)
    eng = engine if engine is not None else get_engine()
    with Session(eng) as session:
        total: int = session.scalar(select(func.count()).select_from(CallLog)) or 0
        if total <= keep:
            return 0
        keep_ids = (
            select(CallLog.call_id)
            .order_by(desc(CallLog.started_at))  # type: ignore[arg-type]
            .limit(keep)
        )
        session.exec(
            delete(CallLog).where(CallLog.call_id.not_in(keep_ids))  # type: ignore[call-overload]
        )
        session.commit()
        return total - keep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def hash_api_key(key: str) -> str:
    """Return the sha256 hex digest of `key` — used as the storage key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
