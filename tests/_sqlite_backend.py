"""SQLite backend for tests only.

Provides ``SqliteBackend`` — a ``DatabaseBackend`` implementation backed
by a SQLite ``ConnectionPool``. Used by test fixtures that need a
self-contained database without a running Postgres instance.
"""

from __future__ import annotations

from foresight.backend.base import DatabaseBackend
from foresight.config import DB_PATH
from foresight.connection_pool import ConnectionPool, CustomRow


class SqliteBackend(DatabaseBackend):
    """DatabaseBackend implementation backed by a SQLite ConnectionPool (tests only)."""

    def __init__(
        self,
        db_path: str | None = None,
        max_size: int = 10,
        max_idle_seconds: int = 300,
    ) -> None:
        self._db_path = db_path
        self._max_size = max_size
        self._max_idle_seconds = max_idle_seconds
        self._pool: ConnectionPool | None = None
        self._backend_type = "sqlite"
        self.row_factory = CustomRow

    def connect(self) -> None:
        path = self._db_path or DB_PATH
        self._pool = ConnectionPool(
            db_path=path,
            max_size=self._max_size,
            max_idle_seconds=self._max_idle_seconds,
        )
        self._backend_type = "sqlite"

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close_all()
            self._pool = None

    def connection(self):
        from contextlib import contextmanager

        if self._pool is None:
            raise RuntimeError("SqliteBackend not connected. Call connect() first.")

        @contextmanager
        def _ctx():
            assert self._pool is not None
            with self._pool.acquire() as conn:
                try:
                    yield conn
                except Exception:
                    conn.rollback()
                    raise

        return _ctx()

    def execute(self, sql: str, params: tuple | dict = ()) -> None:
        if self._pool is None:
            raise RuntimeError("SqliteBackend not connected. Call connect() first.")
        with self._pool.acquire() as conn:
            conn.execute(sql, params)
            conn.commit()

    def execute_many(self, sql: str, params_list: list[tuple]) -> None:
        if self._pool is None:
            raise RuntimeError("SqliteBackend not connected. Call connect() first.")
        with self._pool.acquire() as conn:
            conn.executemany(sql, params_list)
            conn.commit()

    def fetch(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        if self._pool is None:
            raise RuntimeError("SqliteBackend not connected. Call connect() first.")
        with self._pool.acquire() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def fetch_one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        if self._pool is None:
            raise RuntimeError("SqliteBackend not connected. Call connect() first.")
        with self._pool.acquire() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def table_exists(self, table_name: str) -> bool:
        result = self.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        )
        return result is not None

    def column_exists(self, table_name: str, column_name: str) -> bool:
        if not self.table_exists(table_name):
            return False
        rows = self.fetch(f"PRAGMA table_info({table_name})")
        return any(row["name"] == column_name for row in rows)

    def get_version(self) -> int:
        if not self.table_exists("schema_migrations"):
            return 0
        row = self.fetch_one("SELECT MAX(version) AS version FROM schema_migrations")
        return int(row["version"]) if row and row["version"] is not None else 0

    def set_version(self, version: int, applied_at: str) -> None:
        self.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, applied_at),
        )

    @property
    def stats(self) -> dict:
        if self._pool is None:
            return {"idle": 0, "in_use": 0, "max_size": self._max_size}
        return self._pool.stats
