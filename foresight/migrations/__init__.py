"""Database migrations runner.

Runs pending schema migrations in order, recording each in the
schema_migrations table so they are idempotent across restarts.
"""

from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from foresight.backend.backend_migrations import ensure_schema_migrations_table

if TYPE_CHECKING:
    from foresight.backend.base import DatabaseBackend

logger = logging.getLogger(__name__)

MIGRATIONS = [
    (1, "foresight.migrations.001_add_tenant_to_graph_tables"),
    (2, "foresight.migrations.002_unified_schema"),
    (14, "foresight.migrations.014_memory_merge_history"),
]


def run_migrations(backend: DatabaseBackend) -> None:
    """Run all pending migrations against the given backend."""
    # Reconcile any wrong-shaped schema_migrations table (e.g. the app's
    # id/name/executed_at shape) before reading applied versions, so a shared
    # database can never crash run_migrations with UndefinedColumn.
    ensure_schema_migrations_table(backend)

    applied = {row["version"] for row in backend.fetch("SELECT version FROM schema_migrations")}

    for version, module_path in MIGRATIONS:
        if version not in applied:
            mod = importlib.import_module(module_path)
            mod.migrate(backend)
            backend.set_version(version, datetime.now(timezone.utc).isoformat())
            logger.info(f"Applied migration {version}")
