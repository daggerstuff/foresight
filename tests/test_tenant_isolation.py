"""Tests for tenant isolation in Foresight memory system using store_memory and query_memories."""

import contextlib
import os
import sqlite3
import tempfile
from unittest.mock import patch

from foresight.server import (
    QueryMemoriesParams,
    StoreMemoryParams,
    init_db,
    query_memories,
    store_memory,
)
from foresight.tenant_context import (
    reset_tenant_context,
    set_current_tenant_id,
)
from tests._sqlite_backend import SqliteBackend


def _make_temp_db(db_path: str) -> None:
    """Create a fresh temporary database and run full migrations."""

    backend = SqliteBackend(db_path=db_path)
    backend.connect()
    try:
        init_db(backend=backend)
    finally:
        backend.close()


def test_tenant_isolation():
    """Tests that memories are isolated by tenant ID in store_memory and query_memories."""
    # Create temporary DBs for two tenants
    tenant_db1_fd, tenant_db1 = tempfile.mkstemp(suffix=".db")
    tenant_db2_fd, tenant_db2 = tempfile.mkstemp(suffix=".db")
    os.close(tenant_db1_fd)
    os.close(tenant_db2_fd)
    _make_temp_db(tenant_db1)
    _make_temp_db(tenant_db2)

    # Simulate Tenant A
    set_current_tenant_id("tenant_a")
    with (
        patch("foresight.server.get_db_connection", lambda: sqlite3.connect(tenant_db1)),
        patch("foresight.server.BANK_ID", "tenant_a"),
    ):
        result = store_memory(StoreMemoryParams(content="Confidential data for tenant A", user_id="user_a"))
        assert "Stored" in result

    # Simulate Tenant B
    set_current_tenant_id("tenant_b")
    with (
        patch("foresight.server.get_db_connection", lambda: sqlite3.connect(tenant_db2)),
        patch("foresight.server.BANK_ID", "tenant_b"),
    ):
        result = store_memory(StoreMemoryParams(content="Confidential data for tenant B", user_id="user_b"))
        assert "Stored" in result

    # Verify Tenant A cannot see Tenant B memory
    set_current_tenant_id("tenant_a")
    with (
        patch("foresight.server.get_db_connection", lambda: sqlite3.connect(tenant_db1)),
        patch("foresight.server.BANK_ID", "tenant_a"),
    ):
        results = query_memories(QueryMemoriesParams(query="tenant_b"))  # Search for tenant B memory
        assert "tenant b" not in results.lower(), "Tenant A should NOT see Tenant B's memories"

    # Verify Tenant B cannot see Tenant A memory
    set_current_tenant_id("tenant_b")
    with (
        patch("foresight.server.get_db_connection", lambda: sqlite3.connect(tenant_db2)),
        patch("foresight.server.BANK_ID", "tenant_b"),
    ):
        results = query_memories(QueryMemoriesParams(query="tenant_a"))
        assert "tenant a" not in results.lower(), "Tenant B should NOT see Tenant A's memories"

    # Cleanup
    with contextlib.suppress(OSError):
        os.unlink(tenant_db1)
    with contextlib.suppress(OSError):
        os.unlink(tenant_db2)

    reset_tenant_context()
