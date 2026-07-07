"""Connection Pool for Foresight MCP.

When ``FORESIGHT_DB_URL`` is set and ``_initialize_backend()`` has wired
``_global_backend`` to a ``PostgresBackend``, ``get_pool()`` returns a
``_PsycopgPoolAdapter`` that yields ``PostgresPooledConnection`` objects
(see ``server.py``).  The 22 callers that use ``get_pool().acquire()`` /
``pool.release(conn)`` keep working unchanged because the adapter honours
that exact surface.

When no Postgres backend is active, the legacy SQLite ``ConnectionPool``
is returned for test-fixture compatibility (per AD4 — tests stay on
SQLite for speed).
"""

from __future__ import annotations

import importlib
import logging
import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import suppress
from typing import Any

from .config import DB_PATH

logger = logging.getLogger("foresight_connection_pool")


class ConnectionPool:
    """Thread-safe SQLite connection pool."""

    def __init__(self, db_path: str = DB_PATH, max_size: int = 10, max_idle_seconds: int = 300):
        self.db_path = db_path
        self.max_size = max_size
        self.max_idle_seconds = max_idle_seconds
        self._pool: deque[tuple[sqlite3.Connection, float]] = deque()  # (conn, last_used)
        self._in_use: set[sqlite3.Connection] = set()
        self._lock = threading.Lock()

    def acquire(self) -> PooledConnection:
        """Get a connection from the pool."""
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

    def release(self, conn: sqlite3.Connection | PooledConnection) -> None:
        """Return a connection to the pool."""
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

    def _new_connection(self) -> sqlite3.Connection:
        """Create a new database connection with proper settings."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def close_all(self) -> None:
        """Close all connections (for shutdown/testing)."""
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
        """Pool statistics."""
        with self._lock:
            return {
                "idle": len(self._pool),
                "in_use": len(self._in_use),
                "max_size": self.max_size,
            }


def _active_postgres_pool() -> Any | None:
    try:
        _server = importlib.import_module("foresight_mcp.server")
        backend = getattr(_server, "_global_backend", None)
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
        _server = importlib.import_module("foresight_mcp.server")
        raw_conn = self._pool.connection()
        return _server.PostgresPooledConnection(raw_conn, self._pool)

    def release(self, conn: Any) -> None:
        try:
            conn.close()
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


def get_pool(db_path: str | None = None) -> Any:
    """Return the active pool.

    Returns a ``_PsycopgPoolAdapter`` when a Postgres backend is active,
    otherwise the legacy SQLite ``ConnectionPool`` keyed by ``db_path`` (used by tests).
    """
    pg_pool = _active_postgres_pool()
    if pg_pool is not None:
        return _PsycopgPoolAdapter(pg_pool)

    with _pool_lock:
        pool_path = os.path.abspath(db_path or DB_PATH)
        if pool_path not in _pools:
            _pools[pool_path] = ConnectionPool(pool_path)
        return _pools[pool_path]


def reset_pool() -> None:
    """Reset the global pool (for testing)."""
    with _pool_lock:
        for pool in _pools.values():
            pool.close_all()
        _pools.clear()


class PooledConnection:
    """Wraps a sqlite3.Connection so .close() returns it to the pool.

    All attribute access is delegated to the underlying connection,
    but calling .close() releases the connection back to the pool
    instead of truly closing it.
    """

    def __init__(self, conn: sqlite3.Connection, pool: ConnectionPool):
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
