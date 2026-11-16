"""Migrate data from SQLite (~/.foresight/memory.db) to Ghost Postgres.

Idempotent: uses INSERT ... ON CONFLICT DO NOTHING for all tables.
"""

import os
import re
import sqlite3

import psycopg2

SQLITE_PATH = os.path.expanduser("~/.foresight/memory.db")
GHOST_DB_URL = os.environ.get("FORESIGHT_DB_URL")
if not GHOST_DB_URL:
    raise SystemExit("FORESIGHT_DB_URL must be set to the (rotated) Ghost Postgres DSN")

# Tables with data, ordered by FK dependency
TABLES = [
    "tenants",
    "memories",
    "memory_versions",
    "context_blocks",
    "curation_runs",
    "hooks",
    "memory_entities",
    "entity_relationships",
    "schema_migrations",
]


_PLAIN_IDENTIFIER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _quote_identifier(name):
    """Double-quote *name* after proving it is a plain identifier."""
    if not isinstance(name, str) or not _PLAIN_IDENTIFIER.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def _table(table):
    """Validate *table* against the migration's closed table list, then quote it."""
    if table not in TABLES:
        raise ValueError(f"Refusing to migrate unknown table: {table!r}")
    return _quote_identifier(table)


def get_columns(cursor, table):
    cursor.execute("SELECT c.name FROM pragma_table_info(?) c", (table,))
    return [row[0] for row in cursor.fetchall()]


def migrate_table(cur_sqlite, cur_pg, table, conflict_cols=None):
    """Read all rows from SQLite table and insert into Postgres.

    Uses ON CONFLICT (conflict_cols) DO NOTHING for idempotency.
    """
    cols = get_columns(cur_sqlite, table)
    if not cols:
        print(f"  [SKIP] {table}: no columns found")
        return

    cur_sqlite.execute("".join(("SELECT * FROM ", _table(table))))
    rows = cur_sqlite.fetchall()
    if not rows:
        print(f"  [SKIP] {table}: 0 rows")
        return

    # Schema_migrations: only insert versions not already in Postgres
    if table == "schema_migrations":
        cur_pg.execute("SELECT version FROM schema_migrations")
        existing = {r[0] for r in cur_pg.fetchall()}
        rows = [r for r in rows if r[0] not in existing]
        if not rows:
            print(f"  [SKIP] {table}: all {len(cur_sqlite.fetchall())} versions already present")
            # Re-fetch for accurate count
            cur_sqlite.execute("SELECT * FROM schema_migrations")
            orig_count = len(cur_sqlite.fetchall())
            print(f"    (kept {len(existing)} existing, skipped {orig_count - len(rows)})")
            return

    placeholders = ", ".join(["%s"] * len(cols))
    col_names = ", ".join(_quote_identifier(c) for c in cols)
    conflict_target = ", ".join(_quote_identifier(c) for c in (conflict_cols or cols))

    sql = "".join(
        (
            "INSERT INTO ",
            _table(table),
            " (",
            col_names,
            ") VALUES (",
            placeholders,
            ") ON CONFLICT (",
            conflict_target,
            ") DO NOTHING",
        )
    )

    inserted = 0
    skipped = 0
    for row in rows:
        try:
            cur_pg.execute(sql, row)
            if cur_pg.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"  [ERROR] {table}: {e}")
            print(f"    row: {row}")

    print(f"  {table}: {inserted} inserted, {skipped} skipped (of {len(rows)} total)")


def main():
    print(f"SQLite: {SQLITE_PATH}")
    print("Postgres: (Ghost)")

    conn_sqlite = sqlite3.connect(SQLITE_PATH)
    cur_sqlite = conn_sqlite.cursor()

    conn_pg = psycopg2.connect(GHOST_DB_URL)
    conn_pg.autocommit = True
    cur_pg = conn_pg.cursor()

    # Conflict columns per table
    conflict_cols = {
        "memories": ["id"],
        "tenants": ["id"],
        "memory_versions": ["id"],
        "context_blocks": ["tenant_id", "user_id", "label"],
        "curation_runs": ["id"],
        "hooks": ["id"],
        "memory_entities": ["tenant_id", "user_id", "name", "entity_type"],
        "entity_relationships": [
            "tenant_id",
            "user_id",
            "source_entity_id",
            "target_entity_id",
            "relationship_type",
        ],
        "schema_migrations": ["version"],
    }

    for table in TABLES:
        print(f"\n--- {table} ---")
        migrate_table(cur_sqlite, cur_pg, table, conflict_cols.get(table))

    conn_sqlite.close()
    conn_pg.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
