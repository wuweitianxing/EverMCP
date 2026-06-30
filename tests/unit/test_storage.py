"""Unit tests for the SQLite persistence layer (S0 base)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlmodel import Session

from evermcp.storage import (
    DEFAULT_DB_URL,
    InlineCapability,
    get_engine,
    hash_api_key,
    init_db,
    list_inline_capabilities,
)


# ---------------------------------------------------------------------------
# Engine / schema
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_init_db_idempotent(self, tmp_path: Path) -> None:
        """Calling init_db twice on the same engine must not raise."""
        eng = get_engine(f"sqlite:///{tmp_path}/test.db")
        init_db(eng)  # creates schema
        init_db(eng)  # must not raise

    def test_init_db_returns_engine(self, tmp_path: Path) -> None:
        eng = get_engine(f"sqlite:///{tmp_path}/test.db")
        out = init_db(eng)
        assert out is eng


# ---------------------------------------------------------------------------
# list_inline_capabilities
# ---------------------------------------------------------------------------

class TestListInlineCapabilities:
    def test_empty_db_returns_empty_list(self, tmp_path: Path) -> None:
        eng = get_engine(f"sqlite:///{tmp_path}/empty.db")
        init_db(eng)
        assert list_inline_capabilities(eng) == []

    def test_insert_and_list(self, tmp_path: Path) -> None:
        eng = get_engine(f"sqlite:///{tmp_path}/insert.db")
        init_db(eng)
        with Session(eng) as session:
            cap = InlineCapability(
                kind="tool",
                name="demo",
                description="Demo tool",
                schema_json='{"type":"object"}',
            )
            session.add(cap)
            session.commit()

        rows = list_inline_capabilities(eng)
        assert len(rows) == 1
        assert rows[0].name == "demo"
        assert rows[0].kind == "tool"
        assert rows[0].description == "Demo tool"

    def test_disabled_rows_skipped(self, tmp_path: Path) -> None:
        eng = get_engine(f"sqlite:///{tmp_path}/disabled.db")
        init_db(eng)
        with Session(eng) as session:
            session.add(
                InlineCapability(
                    kind="tool",
                    name="enabled_one",
                    description="",
                    enabled=True,
                )
            )
            session.add(
                InlineCapability(
                    kind="tool",
                    name="disabled_one",
                    description="",
                    enabled=False,
                )
            )
            session.commit()

        rows = list_inline_capabilities(eng)
        names = [r.name for r in rows]
        assert "enabled_one" in names
        assert "disabled_one" not in names


# ---------------------------------------------------------------------------
# hash_api_key
# ---------------------------------------------------------------------------

class TestHashApiKey:
    def test_hash_matches_sha256(self) -> None:
        """hash_api_key('foo') == sha256('foo').hexdigest()."""
        assert hash_api_key("foo") == hashlib.sha256(b"foo").hexdigest()

    def test_hash_is_hex_string(self) -> None:
        h = hash_api_key("any-key")
        assert isinstance(h, str)
        assert len(h) == 64  # sha256 hex digest length
        int(h, 16)  # raises if not hex

    def test_hash_is_deterministic(self) -> None:
        assert hash_api_key("stable") == hash_api_key("stable")

    def test_hash_differs_per_input(self) -> None:
        assert hash_api_key("alpha") != hash_api_key("beta")

    def test_known_sha256_digest_prefix(self) -> None:
        # sha256("foo") = 2c26b46b... (well-known fixture).
        assert hash_api_key("foo").startswith("2c26b46b")


# ---------------------------------------------------------------------------
# DEFAULT_DB_URL
# ---------------------------------------------------------------------------

class TestDefaultDbUrl:
    def test_starts_with_sqlite(self) -> None:
        assert DEFAULT_DB_URL.startswith("sqlite:")

    def test_ends_with_db(self) -> None:
        assert DEFAULT_DB_URL.endswith(".db")