"""Connection Pool for Foresight MCP.

When ``FORESIGHT_DB_URL`` is set and ``_initialize_backend()`` has wired
``_global_backend`` to a ``PostgresBackend``, ``get_pool()`` returns a
``_PsycopgPoolAdapter`` that yields ``PostgresPooledConnection`` objects.
This module also owns the process-wide backend holder and the
``get_db_connection()`` entry point, so modules lower in the import graph
than ``server.py`` can obtain connections without importing it.
The 22 callers that use ``get_pool().acquire()`` /
``pool.release(conn)`` keep working unchanged because the adapter honours
that exact surface.

When no Postgres backend is active, ``get_pool()`` requires an explicit
``db_path`` argument (used by test fixtures).  Production calls without
``db_path`` raise ``RuntimeError`` — Postgres is mandatory in production.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from contextlib import suppress
from typing import Any

import psycopg
import psycopg.errors

from .backend.postgres_backend import _translate_sql
from .config import DB_PATH

logger = logging.getLogger("foresight_connection_pool")


class CustomRow:
    """Row wrapper for SQLite connections (test-only)."""

    def __init__(self, cursor, row):

        self._row = sqlite3.Row(cursor, row)

    def get(self, key, default=None):
        try:
            val = self._row[key]
            return val if val is not None else default
        except (IndexError, KeyError):
            return default

    def __getitem__(self, key):
        return self._row[key]

    def __iter__(self):
        return iter(self._row)

    def __len__(self):
        return len(self._row)

    def keys(self):
        return self._row.keys()


class ConnectionPool:
    """Thread-safe SQLite connection pool (test-only)."""

    def __init__(self, db_path: str | None = DB_PATH, max_size: int = 10, max_idle_seconds: int = 300):
        self.db_path = db_path
        self.max_size = max_size
        self.max_idle_seconds = max_idle_seconds
        self._pool: deque[tuple[Any, float]] = deque()
        self._in_use: set[Any] = set()
        self._lock = threading.Lock()

    def acquire(self) -> Any:
        with self._lock:
            while self._pool:
                raw, last_used = self._pool.popleft()
                if time.time() - last_used > self.max_idle_seconds:
                    with suppress(Exception):
                        raw.close()
                    continue
                try:
                    raw.execute("SELECT 1")
                    self._in_use.add(raw)
                    return PooledConnection(raw, self)
                except Exception:
                    with suppress(Exception):
                        raw.close()
                    continue

            if len(self._in_use) >= self.max_size:
                raise RuntimeError(f"Connection pool exhausted ({self.max_size} connections in use)")
            conn = self._new_connection()
            self._in_use.add(conn)
            return PooledConnection(conn, self)

    def release(self, conn: Any) -> None:
        if isinstance(conn, PooledConnection):
            if conn._released:
                return
            conn._released = True
            raw = conn._conn
        else:
            raw = conn

        with self._lock:
            if raw not in self._in_use:
                with suppress(Exception):
                    raw.close()
                return

            self._in_use.discard(raw)
            if len(self._pool) < self.max_size and not any(stored is raw for stored, _ in self._pool):
                try:
                    raw.execute("SELECT 1")
                    self._pool.append((raw, time.time()))
                    return
                except Exception:
                    logger.debug("Connection health check failed on release; closing connection")
            with suppress(Exception):
                raw.close()

    def _new_connection(self) -> Any:

        if self.db_path is None:
            raise ValueError("db_path required for SQLite connection")
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = CustomRow

        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def close_all(self) -> None:
        with self._lock:
            for conn, _ in self._pool:
                with suppress(Exception):
                    conn.close()
            for conn in list(self._in_use):
                with suppress(Exception):
                    conn.close()
            self._pool.clear()
            self._in_use.clear()

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "idle": len(self._pool),
                "in_use": len(self._in_use),
                "max_size": self.max_size,
            }


def _active_postgres_pool() -> Any | None:
    try:
        backend = get_backend()
    except Exception:  # pragma: no cover - defensive
        return None
    if backend is None:
        return None
    if getattr(backend, "_backend_type", None) != "postgresql":
        return None
    return getattr(backend, "_pool", None)


class _PsycopgPoolAdapter:
    """Adapts psycopg_pool.ConnectionPool to the acquire()/release() shape used by 22 callers."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    def acquire(self) -> Any:
        raw_conn = self._pool.getconn()
        return PostgresPooledConnection(raw_conn, self)

    def release(self, conn: Any) -> None:
        try:
            raw = conn._conn if hasattr(conn, "_conn") else conn
            # Commit before returning to the pool: psycopg connections default
            # to non-autocommit, so even SELECT-only use leaves the connection
            # INTRANS. putconn() on a dirty connection logs "rolling back
            # returned connection" and discards uncommitted writes from code
            # paths that rely on implicit commits.
            with suppress(Exception):
                raw.commit()
            self._pool.putconn(raw)
        except Exception:  # pragma: no cover - defensive
            logger.debug("release() failed to close PostgresPooledConnection", exc_info=True)

    @property
    def stats(self) -> dict:
        try:
            idle = self._pool._pool.free()
            in_use = self._pool._pool.size() - idle
        except Exception:
            idle, in_use = 0, 0
        return {"idle": idle, "in_use": in_use, "max_size": getattr(self._pool, "_max_pool_size", None)}


_pools: dict[str, ConnectionPool] = {}
_pool_lock = threading.Lock()


def _is_connection_url(value: str) -> bool:
    """True when ``value`` is a database URL rather than a filesystem path."""
    return value.startswith(("postgresql://", "postgres://"))


def get_pool(db_path: str | None = None) -> Any:
    """Return the active pool.

    * When ``db_path`` is a Postgres URL → routed to the active Postgres pool,
      because a connection URL is not a filesystem path.
    * When ``db_path`` is an explicit path (tests only) → returns the
      legacy SQLite ``ConnectionPool`` keyed on the absolute path.
    * When a Postgres backend is active and no ``db_path`` is given → returns
      a ``_PsycopgPoolAdapter``.
    * When neither condition is met → raises ``RuntimeError``. Production
      must have Postgres; tests must pass an explicit path.
    """
    if db_path is not None and not _is_connection_url(db_path):
        with _pool_lock:
            pool_path = os.path.abspath(db_path)
            if pool_path not in _pools:
                _pools[pool_path] = ConnectionPool(pool_path)
            return _pools[pool_path]

    pg_pool = _active_postgres_pool()
    if pg_pool is not None:
        return _PsycopgPoolAdapter(pg_pool)

    if db_path is not None:
        # A Postgres URL was requested but no backend is initialized. Falling
        # back to SQLite here would silently write to a different store.
        raise RuntimeError(
            "Postgres URL requested but no Postgres backend is active. "
            "Initialize the backend (server._initialize_backend()) before "
            "acquiring a pool."
        )

    raise RuntimeError(
        "No Postgres backend active and no db_path provided. "
        "Production: set FORESIGHT_DB_URL and ensure the server "
        "initialized correctly. Tests: pass an explicit db_path."
    )


def reset_pool() -> None:
    """Reset the global pool (for testing)."""
    with _pool_lock:
        for pool in _pools.values():
            pool.close_all()
        _pools.clear()


class PooledConnection:
    """Wraps a connection so .close() returns it to the pool.

    All attribute access is delegated to the underlying connection,
    but calling .close() releases the connection back to the pool
    instead of truly closing it.
    """

    def __init__(self, conn: Any, pool: ConnectionPool):
        self._conn = conn
        self._pool = pool
        self._released = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    @property
    def row_factory(self) -> Any:
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value: Any) -> None:
        self._conn.row_factory = value

    def close(self):
        if self._released:
            return
        self._pool.release(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __del__(self):
        with suppress(Exception):
            self.close()


class _BackendHolder:
    """Process-wide slot for the database backend (set during startup, PIX-3994)."""

    instance: Any = None


def get_backend() -> Any:
    """Return the active database backend, or ``None`` before startup."""
    return _BackendHolder.instance


def set_backend(backend: Any) -> None:
    """Install (or clear) the process-wide database backend."""
    _BackendHolder.instance = backend


class PostgresPooledConnection(PooledConnection):
    """Wraps a psycopg connection to match the PooledConnection interface.

    Translates SQLite-flavoured SQL to PostgreSQL dialect on execute().
    Used when FORESIGHT_DB_URL is set and PostgresBackend is active.
    Calling close() returns the connection to the psycopg pool.
    """

    def __init__(self, conn: Any, pool: Any) -> None:
        self._conn = conn
        self._pool: Any = pool
        self._last_result = None
        self._rowcount = -1
        self._released = False

    @staticmethod
    def _adapt_row(row: Any) -> Any:
        if row is None or not isinstance(row, dict):
            return row
        return PostgresRow(row)

    def _ensure_healthy_conn(self):
        """Replace the wrapped connection if Neon or the server closed it."""
        if self._conn.closed or getattr(self._conn, "broken", False):
            if hasattr(self._pool, "putconn"):
                with suppress(Exception):
                    self._pool.putconn(self._conn)
            self._conn = self._pool.getconn()

    @staticmethod
    def _is_transient_conn_error(exc: Exception) -> bool:
        """True if *exc* is a connection-level error worth a single retry.

        Covers Neon ``AdminShutdown`` (PG class 57P01), generic ``OperationalError``
        (connection lost mid-query), and ``InterfaceError`` (connection already
        closed/broken).  These are transient: a fresh connection from the pool
        will succeed.
        """
        transient_types = (
            psycopg.OperationalError,
            psycopg.InterfaceError,
            psycopg.errors.AdminShutdown,
        )
        return isinstance(exc, transient_types)

    def _execute_with_retry(self, pg_sql: str, params: tuple) -> Any:
        """Execute *pg_sql* with one automatic retry on transient connection errors.

        Neon serverless Postgres can ``pg_terminate_backend()`` a pooled
        connection between the ``_ensure_healthy_conn()`` check and the actual
        ``execute()`` call (scale-to-zero wake-up race).  This wraps the execute
        in a try/except, refreshes the connection on a transient error, and
        retries exactly once.  A second failure propagates to the caller.
        """
        try:
            return self._conn.execute(pg_sql, params)
        except Exception as exc:
            if not self._is_transient_conn_error(exc):
                raise
            logging.warning("Transient connection error on execute(), refreshing connection: %s", exc)
            self._ensure_healthy_conn()
            return self._conn.execute(pg_sql, params)

    def execute(self, sql, params=()):
        self._ensure_healthy_conn()
        sql_upper = sql.lstrip().upper()
        if sql_upper.startswith("PRAGMA "):
            match = re.search(r"PRAGMA\s+table_info\s*\(\s*([\'\"]?\w+[\'\"]?)\s*\)", sql, re.IGNORECASE)
            if match:
                table_name = match.group(1).strip("'\"")
                pg_sql = (
                    "SELECT 0 as cid, column_name as name, 'TEXT' as type, 0 as notnull, NULL "
                    "as dflt_value, 0 as pk FROM information_schema.columns WHERE table_name = %s"
                )
                self._last_result = self._execute_with_retry(pg_sql, (table_name,))
                self._rowcount = self._last_result.rowcount
                return self

            class DummyResult:
                rowcount = 0

                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

            self._last_result = DummyResult()
            self._rowcount = 0
            return self

        pg_sql = _translate_sql(sql)
        self._last_result = self._execute_with_retry(pg_sql, params)
        self._rowcount = self._last_result.rowcount
        return self

    def execute_returning(self, sql, params=()):
        pg_sql = _translate_sql(sql)
        upper_sql = pg_sql.upper()
        if " RETURNING " not in upper_sql and upper_sql.lstrip().startswith(("INSERT ", "UPDATE ", "DELETE ")):
            pg_sql = f"{pg_sql} RETURNING *"
        self._last_result = self._execute_with_retry(pg_sql, params)
        self._rowcount = self._last_result.rowcount
        return self

    @property
    def rowcount(self):
        return self._rowcount

    def fetchone(self):
        if self._last_result is None:
            return None
        return self._adapt_row(self._last_result.fetchone())

    def fetchall(self):
        if self._last_result is None:
            return []
        return [self._adapt_row(row) for row in self._last_result.fetchall()]

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if not self._released:
            self._released = True
            # Commit any pending transaction before returning to the pool.
            # Without this, the pool rolls back open transactions (INTRANS),
            # discarding writes from code paths that don't call commit() explicitly.
            with suppress(Exception):
                self._conn.commit()
            if hasattr(self._pool, "release"):
                self._pool.release(self)
            elif hasattr(self._pool, "putconn"):
                self._pool.putconn(self._conn)

    @property
    def row_factory(self):
        return dict

    @row_factory.setter
    def row_factory(self, value):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class PostgresRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _supports_execute_returning(conn: Any) -> bool:
    return hasattr(conn, "execute_returning") and callable(conn.execute_returning)


def get_db_connection():
    """Get a Postgres connection from the shared backend pool.

    Foresight is Postgres-only. The backend must be initialized first via
    ``_initialize_backend()`` (which requires ``FORESIGHT_DB_URL``). There is
    deliberately no SQLite fallback: if the backend is not a Postgres backend,
    this raises ``RuntimeError`` instead of silently opening a local SQLite
    store. That dual-store behavior is exactly the bug this migration removes.

    Returns
    -------
    PostgresPooledConnection
        A connection-like object with ``execute()``, ``fetchone()``,
        ``fetchall()``, ``commit()``, and ``close()``.
    """
    backend = get_backend()
    if backend is None:
        raise RuntimeError(
            "Database backend not initialized. Call _initialize_backend() first "
            "(requires FORESIGHT_DB_URL). Foresight is Postgres-only; no SQLite fallback."
        )
    if backend.backend_type != "postgresql":
        raise RuntimeError(
            f"Database backend is '{backend.backend_type}', expected 'postgresql'. "
            "Foresight is Postgres-only; no SQLite fallback."
        )
    _log = logging.getLogger("foresight.db")
    _log.debug("get_db_connection: using PostgresBackend pool")
    pool = backend._pool
    conn = pool.getconn()
    return PostgresPooledConnection(conn, pool)
