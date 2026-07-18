"""Shared test fixtures for Foresight Postgres backend."""

from __future__ import annotations

import os

import pytest

# Use the Ghost Postgres with a dedicated test schema for isolation.
_TEST_DB_URL = (
    "postgresql://tsdbadmin:h4ohtmJpE5DSqOwWGgTNCB-45gG6-Eb1@"
    "l1jgvzcieb.epyzl1cudh.db.ghost.build:5432/tsdb?"
    "sslmode=require&options=-csearch_path%3Dforesight_test"
)


@pytest.fixture(scope="session", autouse=True)
def setup_postgres_backend():
    """Initialize the global Postgres backend once per test session."""
    os.environ["FORESIGHT_DB_URL"] = _TEST_DB_URL

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

    from foresight import server as server_module

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


@pytest.fixture(autouse=True)
def reset_test_tables():
    """Truncate all application tables between tests to prevent cross-test bleed."""
    yield

    from foresight import server as server_module

    if server_module._global_backend is None:
        return

    # Get list of tables in the foresight_test schema
    rows = server_module._global_backend.fetch(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'foresight_test'
        AND table_type = 'BASE TABLE'
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
