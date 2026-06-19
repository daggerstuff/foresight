"""Database backend package for Foresight MCP.

Provides the ``DatabaseBackend`` protocol and concrete implementations:

* ``SqliteBackend`` — default, wraps the existing SQLite connection pool
* ``PostgresBackend`` — PostgreSQL via psycopg v3

Use ``create_backend()`` to instantiate the correct backend based on
the ``FORESIGHT_DB_URL`` environment variable.
"""

from __future__ import annotations

from .backend_factory import create_backend
from .base import DatabaseBackend
from .postgres_backend import PostgresBackend
from .redis_companion import RedisCompanion
from .sqlite_backend import SqliteBackend

__all__ = [
    "DatabaseBackend",
    "RedisCompanion",
    "SqliteBackend",
    "PostgresBackend",
    "create_backend",
]
