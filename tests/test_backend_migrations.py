"""Tests for the backend-agnostic migration runner (PIX-3992).

Verifies that ``foresight.backend.backend_migrations.run_migrations``
correctly bootstraps a fresh Schema (versions 1..11) against the SQLite
backend. A clean postgreSQL happy path is exercised only when
``psycopg`` is installed and ``FORESIGHT_DB_URL_TEST`` points at a
reachable DSN; otherwise that case is skipped.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from foresight.backend import SCHEMA_MIGRATIONS
from foresight.backend.backend_migrations import (
    current_version,
    run_migrations,
)
from foresight.backend.sqlite_backend import SqliteBackend

# =============================================================================
# SQLite backend — primary test surface (no external services required)
# =============================================================================


class _TestConnectionPool:
    """Minimal in-memory connection pool satisfying ``DatabaseBackend``."""

    def __init__(self):
        import sqlite3

        self._conn = sqlite3.connect(":memory:")
        self._conn.row_factory = sqlite3.Row

    def acquire(self):
        return self._conn

    def release(self, _conn):
        return

    def close_all(self):
        self._conn.close()

    @property
    def stats(self):
        return {"idle": 1, "in_use": 0, "max_size": 1}


class TestSqliteMigrationRunner:
    def test_empty_db_runs_versions_one_through_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                applied = run_migrations(backend)
                max_version = max(SCHEMA_MIGRATIONS)
                assert applied == list(range(1, max_version + 1))
                assert current_version(backend) == max_version
            finally:
                backend.close()

    def test_idempotent_on_re_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                first = run_migrations(backend)
                second = run_migrations(backend)
                assert first, "first run should apply at least one version"
                assert second == [], "second run should be a no-op"
                assert current_version(backend) == max(SCHEMA_MIGRATIONS)
            finally:
                backend.close()

    def test_key_tables_present_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                run_migrations(backend)
                required = {
                    "tenants",
                    "memories",
                    "memory_versions",
                    "decay_config",
                    "curation_runs",
                    "context_blocks",
                    "memory_relationships",
                    "memory_embeddings",
                    "documents",
                    "document_chunks",
                    "memory_decay_events",
                    "injection_runs",
                    "schema_migrations",
                }
                missing = sorted(name for name in required if not backend.table_exists(name))
                assert missing == [], f"missing tables after migration: {missing}"
            finally:
                backend.close()

    def test_memories_round_trip_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                run_migrations(backend)
                backend.execute(
                    "INSERT INTO memories (id, content, created_at) VALUES (?, ?, ?)",
                    ("mem-1", "hello world", "2026-01-01T00:00:00+00:00"),
                )
                rows = backend.fetch("SELECT content FROM memories WHERE id = ?", ("mem-1",))
                assert len(rows) == 1
                assert rows[0]["content"] == "hello world"
            finally:
                backend.close()

    def test_wrong_shaped_schema_migrations_is_reconciled(self):
        """A pre-existing app-shaped schema_migrations (id/name/executed_at) is
        preserved under a legacy name and recreated with version/applied_at so
        run_migrations no longer crashes with UndefinedColumn."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                # Simulate the shape the pixelated db-migrate tool creates on
                # shared databases (id/name/executed_at), including a row.
                backend.execute("CREATE TABLE schema_migrations (id INTEGER PRIMARY KEY, name TEXT, executed_at TEXT)")
                backend.execute(
                    "INSERT INTO schema_migrations (id, name, executed_at) VALUES (1, '013_consent_records.sql', '2026-07-31')"
                )

                applied = run_migrations(backend)

                assert applied, "migrations should apply after reconcile"
                assert backend.table_exists("schema_migrations_legacy")
                legacy = backend.fetch("SELECT * FROM schema_migrations_legacy")
                assert len(legacy) == 1
                assert legacy[0]["name"] == "013_consent_records.sql"
                # Recreated tracker has the expected shape and new versions.
                assert backend.column_exists("schema_migrations", "version")
                assert backend.column_exists("schema_migrations", "applied_at")
                assert current_version(backend) == max(SCHEMA_MIGRATIONS)
            finally:
                backend.close()

    def test_wrong_shaped_schema_migrations_with_existing_legacy(self):
        """If schema_migrations_legacy already exists, the reconcile uses a
        suffixed name so the rename never collides and data is preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                backend.execute("CREATE TABLE schema_migrations_legacy (version INTEGER)")
                backend.execute("CREATE TABLE schema_migrations (id INTEGER PRIMARY KEY, name TEXT, executed_at TEXT)")
                backend.execute(
                    "INSERT INTO schema_migrations (id, name, executed_at) VALUES (7, '007_x.sql', '2026-07-30')"
                )

                applied = run_migrations(backend)

                assert applied
                assert backend.table_exists("schema_migrations_legacy_2")
                legacy = backend.fetch("SELECT * FROM schema_migrations_legacy_2")
                assert len(legacy) == 1
                assert legacy[0]["name"] == "007_x.sql"
                assert backend.column_exists("schema_migrations", "version")
                assert current_version(backend) == max(SCHEMA_MIGRATIONS)
            finally:
                backend.close()


# =============================================================================
# server.init_db() path — the exact entrypoint that failed in CI (conftest
# setup_postgres_backend → init_db → SELECT version FROM schema_migrations
# crashed with UndefinedColumn on a shared DB with app-shaped schema_migrations).
# =============================================================================


class TestServerInitDbReconcilesWrongShapedSchemaMigrations:
    def test_init_db_reconciles_app_shaped_schema_migrations(self):
        """init_db() must reconcile a pre-existing app-shaped schema_migrations
        (id/name/executed_at) instead of crashing with UndefinedColumn — the
        exact failure class that broke foresight CI on the shared Neon DB."""
        from foresight.server import init_db

        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                # Simulate the shared DB's app-shaped table with a row.
                backend.execute("CREATE TABLE schema_migrations (id INTEGER PRIMARY KEY, name TEXT, executed_at TEXT)")
                backend.execute(
                    "INSERT INTO schema_migrations (id, name, executed_at) VALUES (13, '013_consent_records.sql', '2026-07-31')"
                )
            finally:
                backend.close()

            # Reconnect and run init_db — must not raise UndefinedColumn.
            # (init_db() closes the backend in its finally block, so reconnect
            # before asserting on the resulting schema.)
            backend = SqliteBackend(db_path=db)
            init_db(backend)
            backend.connect()
            try:
                assert backend.column_exists("schema_migrations", "version")
                assert backend.column_exists("schema_migrations", "applied_at")
                legacy = backend.fetch("SELECT * FROM schema_migrations_legacy")
                assert len(legacy) == 1
                assert legacy[0]["name"] == "013_consent_records.sql"
            finally:
                backend.close()


# =============================================================================
# Base-class helpers — shared by both backends, exercised here
# =============================================================================


class TestBackendBaseSchemaHelpers:
    def test_table_exists_true_for_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                backend.execute("CREATE TABLE t_seen (id INTEGER PRIMARY KEY)")
                assert backend.table_exists("t_seen") is True
            finally:
                backend.close()

    def test_table_exists_false_for_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                assert backend.table_exists("definitely_missing_table") is False
            finally:
                backend.close()

    def test_get_version_zero_when_predicate_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "foresight.sqlite")
            backend = SqliteBackend(db_path=db)
            backend.connect()
            try:
                assert backend.get_version() == 0
            finally:
                backend.close()


# =============================================================================
# Psycopg v3 optional test — runs only if the extra is installed AND an env
# var points at a reachable Postgres DSN. This avoids hard-failing CI when
# runners do not have a database available.
# =============================================================================


@pytest.mark.skipif(
    os.environ.get("FORESIGHT_DB_URL_TEST") is None,
    reason="FORESIGHT_DB_URL_TEST not set; skipping Postgres integration test",
)
class TestPostgresMigrationRunner:
    def test_migrations_apply(self):
        dsn = os.environ["FORESIGHT_DB_URL_TEST"]
        from foresight.backend.postgres_backend import PostgresBackend

        backend = PostgresBackend(dsn=dsn)
        backend.connect()
        try:
            backend.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
            backend.execute("DROP TABLE IF EXISTS memories CASCADE")
            applied = run_migrations(backend)
            max_version = max(SCHEMA_MIGRATIONS)
            assert applied == list(range(1, max_version + 1))
            assert current_version(backend) == max_version
            assert backend.table_exists("memories") is True
        finally:
            backend.close()

    def test_wrong_shaped_schema_migrations_is_reconciled(self):
        """Postgres: an app-shaped schema_migrations (id/name/executed_at) is
        preserved under a legacy name and recreated with version/applied_at so
        the runner no longer crashes with UndefinedColumn (the exact failure
        class seen in foresight CI against the shared Neon DB)."""
        dsn = os.environ["FORESIGHT_DB_URL_TEST"]
        from foresight.backend.postgres_backend import PostgresBackend

        backend = PostgresBackend(dsn=dsn)
        backend.connect()
        try:
            backend.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
            backend.execute("DROP TABLE IF EXISTS schema_migrations_legacy CASCADE")
            backend.execute("CREATE TABLE schema_migrations (id INTEGER PRIMARY KEY, name TEXT, executed_at TEXT)")
            backend.execute(
                "INSERT INTO schema_migrations (id, name, executed_at) VALUES (13, '013_consent_records.sql', '2026-07-31')"
            )

            applied = run_migrations(backend)

            assert applied, "migrations should apply after reconcile"
            assert backend.table_exists("schema_migrations_legacy")
            legacy = backend.fetch("SELECT * FROM schema_migrations_legacy")
            assert len(legacy) == 1
            assert legacy[0]["name"] == "013_consent_records.sql"
            assert backend.column_exists("schema_migrations", "version")
            assert backend.column_exists("schema_migrations", "applied_at")
            assert current_version(backend) == max(SCHEMA_MIGRATIONS)
        finally:
            backend.close()
