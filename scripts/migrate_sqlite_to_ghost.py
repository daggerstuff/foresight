"""Migrate data from SQLite to Ghost Postgres.

Idempotent: uses INSERT ... ON CONFLICT DO NOTHING for all tables.
"""

import os
import sqlite3

import psycopg2

SQLITE_PATH = os.path.expanduser(os.environ.get("FORESIGHT_DB_PATH", "~/.foresight/memory.db"))
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

# Conflict columns per table (used for ON CONFLICT DO NOTHING)
CONFLICT_COLS = {
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

# Whitelist for SQL identifier interpolation (preventing injection)
TABLE_WHITELIST = set(TABLES)


def get_columns(cursor, table):
    """Fetch column names from SQLite pragma."""
    cursor.execute(
        "SELECT c.name FROM pragma_table_info(?) c",
        (table,),
    )
    return [row[0] for row in cursor.fetchall()]


def migrate_table(cur_sqlite, cur_pg, table, conflict_cols=None):
    """Read all rows from SQLite table and insert into Postgres.

    Uses ON CONFLICT (conflict_cols) DO NOTHING for idempotency.
    """
    if table not in TABLE_WHITELIST:
        return

    cols = get_columns(cur_sqlite, table)
    if not cols:
        return

    cur_sqlite.execute(f"SELECT * FROM {table}")
    rows = cur_sqlite.fetchall()
    orig_count = len(rows)
    if not rows:
        return

    # Schema_migrations: only insert versions not already in Postgres
    if table == "schema_migrations":
        cur_pg.execute("SELECT version FROM schema_migrations")
        existing = {r[0] for r in cur_pg.fetchall()}
        rows = [r for r in rows if r[0] not in existing]
        orig_count - len(rows)
        if not rows:
            return

    placeholders = ", ".join(["%s"] * len(cols))
    col_names = ", ".join(cols)
    conflict_target = ", ".join(conflict_cols) if conflict_cols else col_names

    sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) ON CONFLICT ({conflict_target}) DO NOTHING"

    inserted = 0
    skipped = 0
    for row in rows:
        try:
            cur_pg.execute(sql, row)
            if cur_pg.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
        except Exception:
            str(row[0])[:80] if row else "<unknown>"


def main():

    # Open SQLite in read-only mode to fail fast if path is wrong
    conn_sqlite = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    cur_sqlite = conn_sqlite.cursor()

    conn_pg = psycopg2.connect(GHOST_DB_URL)
    conn_pg.autocommit = True
    cur_pg = conn_pg.cursor()

    try:
        for table in TABLES:
            migrate_table(cur_sqlite, cur_pg, table, CONFLICT_COLS.get(table))
    finally:
        conn_sqlite.close()
        conn_pg.close()


if __name__ == "__main__":
    main()
