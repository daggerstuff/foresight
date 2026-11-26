"""Shared test fixtures for Foresight Postgres backend.

Tests run against a **dedicated test database** (``foresight_test``) to avoid
wiping production data.  The test DB URL is derived from ``FORESIGHT_DB_URL``
by replacing the database name with ``foresight_test``.  If
``FORESIGHT_TEST_DB_URL`` is set explicitly, that takes precedence.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from foresight import server as server_module
from foresight.capture import reset_capture_pipeline
from foresight.connection_pool import reset_pool
from foresight.event_bus import reset_event_bus
from foresight.graph_store import reset_graph_store
from foresight.hybrid_retriever import reset_hybrid_retriever
from foresight.memory_relationships import reset_memory_relationship_store
from foresight.server import (
    _initialize_backend,
    get_graph_store,
    get_hybrid_retriever,
    get_temporal_query_builder,
    init_db,
)
from foresight.subconscious import _context_block_agents
from foresight.sync import reset_sync_manager
from foresight.temporal_queries import reset_temporal_query_builder

# Load .env before checking for FORESIGHT_DB_URL so that `uv run pytest`
# works without prefixing the env var manually.  Walk up from the tests/
# directory to find the project root .env (same logic as server.py).
try:
    from dotenv import load_dotenv

    for _candidate in [
        Path(__file__).resolve().parent.parent / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",
        Path.home() / ".env",
    ]:
        if _candidate.exists():
            load_dotenv(_candidate, override=True)
            break
except ImportError:
    pass  # python-dotenv not installed; rely on env being set externally

# Derive the test database URL from FORESIGHT_DB_URL by swapping the database
# name to "foresight_test".  This ensures tests never truncate the production
# database.  FORESIGHT_TEST_DB_URL can override the derivation entirely.
_prod_url = os.environ.get("FORESIGHT_DB_URL") or ""
_test_url = os.environ.get("FORESIGHT_TEST_DB_URL") or ""
if not _test_url and _prod_url:
    # Replace the last path segment (database name) with "foresight_test"
    base, _, query = _prod_url.partition("?")
    _test_url = base.rsplit("/", 1)[0] + "/foresight_test"
    if query:
        _test_url += "?" + query

_TEST_DB_URL = _test_url
if not _TEST_DB_URL:
    raise pytest.skip("FORESIGHT_DB_URL not set — skipping tests that require PostgreSQL")


@pytest.fixture(scope="session", autouse=True)
def setup_postgres_backend():
    """Initialize the global Postgres backend once per test session."""
    os.environ["FORESIGHT_DB_URL"] = _TEST_DB_URL
    # Skill artifacts are HMAC-signed and verified fail-closed, so the suite needs a
    # configured secret; tests that assert the unconfigured path delete this variable.
    os.environ.setdefault("FORESIGHT_PROFILE_SIGNING_KEY", "test-profile-signing-key")

    _initialize_backend()
    init_db()

    backend = server_module.get_backend()
    if backend is not None:
        reset_graph_store()
        reset_hybrid_retriever()
        reset_temporal_query_builder()
        get_hybrid_retriever(backend=backend)
        get_graph_store(backend=backend)
        get_temporal_query_builder(backend=backend)

    yield

    # Clean up backend at session end
    backend = server_module.get_backend()
    if backend is not None:
        backend.close()


def _truncate_all_tables() -> None:
    """Truncate every application table in the public schema."""

    backend = server_module.get_backend()
    if backend is None:
        return

    # Get list of tables in the connection's current schema (the app tables
    # live in `public`, not a dedicated `foresight_test` schema). Keep the
    # schema_migrations bookkeeping table intact so init_db() stays idempotent
    # across tests that call it directly.
    rows = backend.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = current_schema()
        AND table_type = 'BASE TABLE'
        AND table_name <> 'schema_migrations'
        """
    )
    tables = [r["table_name"] for r in rows]

    if not tables:
        return

    # Truncate all tables in a single statement so Postgres resolves FK order.
    # CASCADE ensures dependent rows in related tables are also removed.
    with backend.connection() as conn:
        with conn.cursor() as cur:
            # A connection a test left open inside a transaction holds its
            # locks, and TRUNCATE (ACCESS EXCLUSIVE) then waits forever: the CI
            # suite sat here for 37 minutes with no output. Drop those sessions
            # first, and bound the wait so a future leak fails in seconds with
            # the blocking relation named instead of hanging.
            cur.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND state LIKE 'idle in transaction%'
                """
            )
            cur.execute("SET LOCAL lock_timeout = '10s'")
            table_list = ", ".join(f'"{t}"' for t in tables)
            cur.execute(f"TRUNCATE {table_list} CASCADE")
        conn.commit()


def _reset_in_memory_singletons() -> None:
    """Reset module-level singletons so tests start from a clean state."""
    with contextlib.suppress(Exception):
        reset_event_bus()
    with contextlib.suppress(Exception):
        reset_sync_manager()
    with contextlib.suppress(Exception):
        reset_capture_pipeline()
    with contextlib.suppress(Exception):
        reset_pool()
    with contextlib.suppress(Exception):
        reset_memory_relationship_store()
    with contextlib.suppress(Exception):
        _context_block_agents.clear()

    # Re-wire the Postgres-backed singletons. Tests that exercise SQLite
    # fallback paths call reset_hybrid_retriever()/reset_graph_store() without
    # restoring the backend, leaving a backend-less singleton that silently
    # routes later tests into raw SQLite ("no such table: memories").
    try:
        backend = server_module.get_backend()
        if backend is not None:
            reset_graph_store()
            reset_hybrid_retriever()
            reset_temporal_query_builder()
            get_hybrid_retriever(backend=backend)
            get_graph_store(backend=backend)
            get_temporal_query_builder(backend=backend)
    except Exception:
        pass


@pytest.fixture(autouse=True)
def reset_test_tables():
    """Truncate all application tables before AND after each test, and reset
    in-memory singletons, to prevent cross-test state bleed."""
    _truncate_all_tables()
    _reset_in_memory_singletons()

    yield

    _truncate_all_tables()
    _reset_in_memory_singletons()
