"""Database backend package for Foresight MCP.

Provides the ``DatabaseBackend`` protocol and concrete implementations:

* ``SqliteBackend`` — default, wraps the existing SQLite connection pool
* ``PostgresBackend`` — (Phase 2) native asyncpg-backed PostgreSQL

Use the ``create_backend()`` factory to instantiate the correct backend
based on the ``FORESIGHT_DB_URL`` environment variable.
"""

from __future__ import annotations

from .base import DatabaseBackend
from .sqlite_backend import SqliteBackend
from .postgres_backend import PostgresBackend

__all__ = [
    "DatabaseBackend",
    "SqliteBackend",
    "PostgresBackend",
    "create_backend",
]


def create_backend(db_url: str | None = None) -> DatabaseBackend:
    """Create the appropriate database backend based on configuration.

    Parameters
    ----------
    db_url :
        Database URL.  If ``None`` (default) the environment variable
        ``FORESIGHT_DB_URL`` is read.  When neither is set the default
        SqliteBackend is returned.

    Returns
    -------
    DatabaseBackend
        An initialised backend ready for ``connect()``.
    """
    if db_url is None:
        import os

        db_url = os.environ.get("FORESIGHT_DB_URL", "")

    if db_url and db_url.startswith("postgresql://"):
        return PostgresBackend(dsn=db_url)

    return SqliteBackend()
