"""Shared test fixtures for Foresight Postgres backend.

Tests run against a **dedicated test database** (``foresight_test``) to avoid
wiping production data.  The test DB URL is derived from ``FORESIGHT_DB_URL``
by replacing the database name with ``foresight_test``.  If
``FORESIGHT_TEST_DB_URL`` is set explicitly, that takes precedence.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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

    from foresight import server as server_module
    from foresight.graph_store import reset_graph_store
    from foresight.hybrid_retriever import reset_hybrid_retriever
    from foresight.server import (
        _initialize_backend,
        get_graph_store,
        get_hybrid_retriever,
        get_temporal_query_builder,
        init_db,
    )
    from foresight.temporal_queries import reset_temporal_query_builder

    _initialize_backend()
    init_db()

    if server_module._global_backend is not None:
        reset_graph_store()
        reset_hybrid_retriever()
        reset_temporal_query_builder()
        get_hybrid_retriever(backend=server_module._global_backend)
        get_graph_store(backend=server_module._global_backend)
        get_temporal_query_builder(backend=server_module._global_backend)

    yield

    # Clean up backend at session end
    if server_module._global_backend is not None:
        server_module._global_backend.close()


def _truncate_all_tables() -> None:
    """Truncate every application table in the public schema."""
    from foresight import server as server_module

    if server_module._global_backend is None:
        return

    # Get list of tables in the connection's current schema (the app tables
    # live in `public`, not a dedicated `foresight_test` schema). Keep the
    # schema_migrations bookkeeping table intact so init_db() stays idempotent
    # across tests that call it directly.
    rows = server_module._global_backend.fetch(
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
    with server_module._global_backend.connection() as conn:
        with conn.cursor() as cur:
            table_list = ", ".join(f'"{t}"' for t in tables)
            cur.execute(f"TRUNCATE {table_list} CASCADE")
        conn.commit()


def _reset_in_memory_singletons() -> None:
    """Reset module-level singletons so tests start from a clean state."""
    try:
        from foresight.event_bus import reset_event_bus

        reset_event_bus()
    except Exception:
        pass

    try:
        from foresight.sync import reset_sync_manager

        reset_sync_manager()
    except Exception:
        pass

    try:
        from foresight.capture import reset_capture_pipeline

        reset_capture_pipeline()
    except Exception:
        pass

    try:
        from foresight.connection_pool import reset_pool

        reset_pool()
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
