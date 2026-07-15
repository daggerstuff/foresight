"""Drain legacy standalone SQLite files into shared Postgres tables.

Reads existing SQLite files (~/.foresight/operations.db and the
narrative_cache sqlite path) IF they exist, and bulk-inserts their rows
into the Postgres ``operations`` / ``narrative_cache`` tables.

Idempotent: rows whose PRIMARY KEY already exists in Postgres are skipped
via ON CONFLICT DO NOTHING.

Usage:
    uv run python scripts/drain_sqlite_to_postgres.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# ── Locate legacy SQLite files ──────────────────────────────────────────────
HOME = Path.home()
OPERATIONS_DB = HOME / ".foresight" / "operations.db"
NARRATIVE_CACHE_DB = HOME / ".foresight" / "narrative_cache.sqlite"


def _get_pg_conn():
    """Acquire a Postgres connection via get_db_connection()."""
    from foresight.server import get_db_connection

    return get_db_connection()


def drain_operations() -> tuple[int, int]:
    """Drain operations.db → Postgres operations table.

    Returns (migrated, skipped).
    """
    if not OPERATIONS_DB.exists():
        print(f"  [SKIP] {OPERATIONS_DB} not found — nothing to drain")
        return 0, 0

    sqlite_conn = sqlite3.connect(str(OPERATIONS_DB))
    sqlite_conn.row_factory = sqlite3.Row
    rows = sqlite_conn.execute(
        "SELECT id, tenant_id, type, entity_type, entity_id, payload, "
        "created_at, retry_count, last_attempt, vector_clock FROM operations"
    ).fetchall()
    sqlite_conn.close()

    if not rows:
        print(f"  [EMPTY] {OPERATIONS_DB} has no rows")
        return 0, 0

    pg_conn = _get_pg_conn()
    migrated = 0
    skipped = 0
    for row in rows:
        try:
            pg_conn.execute(
                """
                INSERT INTO operations
                (id, tenant_id, type, entity_type, entity_id, payload,
                 created_at, retry_count, last_attempt, vector_clock)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    row["id"],
                    row["tenant_id"] or "default",
                    row["type"],
                    row["entity_type"],
                    row["entity_id"],
                    row["payload"],
                    row["created_at"],
                    row["retry_count"] or 0,
                    row["last_attempt"],
                    row["vector_clock"] or "{}",
                ),
            )
            if pg_conn.rowcount and pg_conn.rowcount > 0:
                migrated += 1
            else:
                skipped += 1
        except Exception as exc:
            print(f"  [WARN] Failed to insert operation {row['id']}: {exc}", file=sys.stderr)
            skipped += 1

    pg_conn.commit()
    pg_conn.close()
    return migrated, skipped


def drain_narrative_cache() -> tuple[int, int]:
    """Drain narrative_cache.sqlite → Postgres narrative_cache table.

    Returns (migrated, skipped).
    """
    if not NARRATIVE_CACHE_DB.exists():
        print(f"  [SKIP] {NARRATIVE_CACHE_DB} not found — nothing to drain")
        return 0, 0

    sqlite_conn = sqlite3.connect(str(NARRATIVE_CACHE_DB))
    sqlite_conn.row_factory = sqlite3.Row
    rows = sqlite_conn.execute(
        "SELECT cache_key, tenant_id, user_id, report_id, model_version, "
        "insights_hash, narrative, created_at, last_accessed_at, access_count "
        "FROM narrative_cache"
    ).fetchall()
    sqlite_conn.close()

    if not rows:
        print(f"  [EMPTY] {NARRATIVE_CACHE_DB} has no rows")
        return 0, 0

    pg_conn = _get_pg_conn()
    migrated = 0
    skipped = 0
    for row in rows:
        try:
            pg_conn.execute(
                """
                INSERT INTO narrative_cache
                (cache_key, tenant_id, user_id, report_id, model_version,
                 insights_hash, narrative, created_at, last_accessed_at, access_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (cache_key) DO NOTHING
                """,
                (
                    row["cache_key"],
                    row["tenant_id"],
                    row["user_id"],
                    row["report_id"],
                    row["model_version"],
                    row["insights_hash"],
                    row["narrative"],
                    row["created_at"],
                    row["last_accessed_at"],
                    row["access_count"] or 0,
                ),
            )
            if pg_conn.rowcount and pg_conn.rowcount > 0:
                migrated += 1
            else:
                skipped += 1
        except Exception as exc:
            print(f"  [WARN] Failed to insert cache entry {row['cache_key']}: {exc}", file=sys.stderr)
            skipped += 1

    pg_conn.commit()
    pg_conn.close()
    return migrated, skipped


def main() -> None:
    db_url = os.environ.get("FORESIGHT_DB_URL")
    if not db_url:
        print("ERROR: FORESIGHT_DB_URL must be set to the shared Ghost Postgres DSN", file=sys.stderr)
        sys.exit(1)

    print("Draining legacy SQLite files → Postgres")
    print(f"  FORESIGHT_DB_URL: {db_url[:50]}...")

    ops_migrated, ops_skipped = drain_operations()
    cache_migrated, cache_skipped = drain_narrative_cache()

    total_migrated = ops_migrated + cache_migrated
    total_skipped = ops_skipped + cache_skipped

    print()
    print("Summary:")
    print(f"  operations:     {ops_migrated} migrated, {ops_skipped} skipped")
    print(f"  narrative_cache: {cache_migrated} migrated, {cache_skipped} skipped")
    print(f"  TOTAL:           {total_migrated} migrated, {total_skipped} skipped")
    print("Done.")


if __name__ == "__main__":
    main()
