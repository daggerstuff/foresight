"""Backend-agnostic migration runner for Foresight MCP.

Runs pending schema migrations against any ``DatabaseBackend`` implementation
and records each applied version in ``schema_migrations``. DDL statements come
from the single source of truth in ``schema_ddl.MIGRATIONS``.

Usage::

    from foresight.backend import create_backend
    from foresight.backend.backend_migrations import run_migrations

    backend = create_backend()
    backend.connect()
    run_migrations(backend)
    backend.close()

The runner is idempotent: re-invoking it on an up-to-date database is a
no-op. Per-version statements are wrapped in a single transaction so a
mid-migration crash is recovered cleanly on the next call.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .base import DatabaseBackend
from .postgres_backend import _translate_sql
from .schema_ddl import MIGRATIONS

logger = logging.getLogger(__name__)


# Substrings that, when present in the database error message, indicate the
# statement is already applied (idempotent on re-run). The PostgreSQL backend
# raises distinct messages for "duplicate column" / "already exists"; SQLite
# raises "OperationalError: duplicate column name" and similar.
_IDEMPOTENT_SQLITE_HINTS: tuple[str, ...] = (
    "duplicate column",
    "already exists",
)
_IDEMPOTENT_PG_HINTS: tuple[str, ...] = (
    "already exists",
    "duplicate column",
)


def _is_idempotent_error(exc: Exception) -> bool:
    """Return True if ``exc`` indicates the statement is already applied."""
    message = str(exc).lower()
    return any(hint in message for hint in _IDEMPOTENT_SQLITE_HINTS + _IDEMPOTENT_PG_HINTS)


def ensure_schema_migrations_table(backend: DatabaseBackend) -> None:
    """Ensure ``schema_migrations`` exists with the expected (version, applied_at) shape.

    Self-healing against shared databases: if a ``schema_migrations`` table
    already exists but was created by a different tool with a different
    column shape (e.g. the pixelated app's ``id/name/executed_at``), the
    migration runner would previously crash with ``UndefinedColumn: column
    "version" does not exist`` because ``CREATE TABLE IF NOT EXISTS`` is a
    no-op and ``SELECT version`` then fails.

    When a wrong-shaped table is detected, it is preserved by renaming it to
    ``schema_migrations_legacy`` (its rows are never destroyed) and a fresh
    ``version/applied_at`` table is created, so ``SELECT version FROM
    schema_migrations`` always works afterwards. The DDL contains no ``?``
    placeholders, so the Postgres dialect translation is a no-op either way.
    """
    if backend.table_exists("schema_migrations") and not (
        backend.column_exists("schema_migrations", "version")
        and backend.column_exists("schema_migrations", "applied_at")
    ):
        # Wrong shape — keep the old data safe under a legacy name, then
        # recreate with the shape this runner (and set_version) expects.
        # Either missing column (version OR applied_at) triggers the
        # reconcile, since SELECT version and the version/applied_at INSERT
        # both require the full shape.
        legacy_name = "schema_migrations_legacy"
        if backend.table_exists(legacy_name):
            # A legacy table already exists from a previous reconcile; pick
            # a suffixed name so the rename below never collides.
            suffix = 2
            while backend.table_exists(f"{legacy_name}_{suffix}"):
                suffix += 1
            legacy_name = f"{legacy_name}_{suffix}"
        backend.execute(f"ALTER TABLE schema_migrations RENAME TO {legacy_name}")
        logger.warning(
            "schema_migrations had an incompatible shape; renamed to %s and recreated",
            legacy_name,
        )

    with backend.connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _applied_versions(backend: DatabaseBackend) -> set[int]:
    """Return the set of migration versions already applied to the backend."""
    if not backend.table_exists("schema_migrations"):
        return set()
    rows = backend.fetch("SELECT version FROM schema_migrations")
    return {int(r["version"]) for r in rows if r.get("version") is not None}


def _applied_at_iso() -> str:
    """Return the current UTC timestamp in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def run_migrations(backend: DatabaseBackend) -> list[int]:
    """Run all pending migrations against ``backend``.

    Returns the list of versions newly applied (in ascending order). If the
    backend is already up to date, returns an empty list.
    """
    ensure_schema_migrations_table(backend)
    applied = _applied_versions(backend)

    newly_applied: list[int] = []
    for version in sorted(MIGRATIONS):
        if version in applied:
            continue

        statements = MIGRATIONS[version]
        try:
            with backend.connection() as conn:
                # The runner executes DDL directly on the connection, so the
                # SQLite-flavoured statements in MIGRATIONS must be translated
                # to the PostgreSQL dialect (raw ``conn.execute`` bypasses
                # PostgresBackend._translate_sql). Wrap each statement in a
                # SAVEPOINT so an idempotent failure (e.g. duplicate column
                # that MIGRATIONS[1] already created) rolls back only that
                # statement. PostgreSQL aborts the whole transaction on any
                # error, so without this the next statement dies with
                # InFailedSqlTransaction.
                for idx, stmt in enumerate(statements):
                    # ALTER COLUMN ... TYPE is Postgres-only; SQLite uses
                    # type affinity so the type change is a no-op there.
                    if backend.backend_type != "postgresql" and "ALTER COLUMN" in stmt.upper():
                        logger.debug("Migration %s: skipping Postgres-only ALTER COLUMN on %s", version, backend.backend_type)
                        continue
                    savepoint = f"mig_v{version}_s{idx}"
                    conn.execute(f"SAVEPOINT {savepoint}")
                    try:
                        conn.execute(_translate_sql(stmt) if backend.backend_type == "postgresql" else stmt)
                    except Exception as exc:
                        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                        if _is_idempotent_error(exc):
                            logger.debug(
                                "Migration %s: skipping already-applied statement (%s)",
                                version,
                                exc,
                            )
                            continue
                        # Non-idempotent failure: restore the outer transaction
                        # state explicitly so the pooled connection is never
                        # returned mid-transaction, then propagate.
                        conn.rollback()
                        raise
                    else:
                        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                conn.commit()
        except Exception:
            logger.exception("Migration %s failed; aborting before any version is recorded", version)
            raise

        backend.set_version(version, _applied_at_iso())
        logger.info("Applied migration %s", version)
        newly_applied.append(version)

    return newly_applied


def current_version(backend: DatabaseBackend) -> int:
    """Return the highest applied schema version (0 if none)."""
    ensure_schema_migrations_table(backend)
    return backend.get_version()


__all__ = ["current_version", "ensure_schema_migrations_table", "run_migrations"]
