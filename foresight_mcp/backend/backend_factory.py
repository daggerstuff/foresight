"""Backend factory — selects and instantiates the correct DatabaseBackend.

``FORESIGHT_DB_URL`` (or an explicit ``db_url`` argument) is **required**.
SQLite as a primary backend is no longer supported — tests use
``SqliteBackend`` directly (see AD4).
"""

from __future__ import annotations

import os

from foresight_mcp.config import DB_URL

from .base import DatabaseBackend
from .postgres_backend import PostgresBackend
from .sqlite_backend import SqliteBackend


def create_backend(db_url: str | None = None) -> DatabaseBackend:
    """Create a PostgresBackend from ``FORESIGHT_DB_URL``.

    Parameters
    ----------
    db_url :
        Database URL.  If ``None`` (default) the environment variable
        ``FORESIGHT_DB_URL`` is read.

    Returns
    -------
    DatabaseBackend
        An **unconnected** ``PostgresBackend`` — call ``.connect()`` before use.

    Raises
    ------
    RuntimeError
        If no Postgres DSN is provided or found in the environment.
    """
    if db_url is None:
        db_url = os.environ.get("FORESIGHT_DB_URL", "") or ""

    if not db_url:
        db_url = DB_URL

    if db_url and db_url.startswith(("postgresql://", "postgres://")):
        return PostgresBackend(dsn=db_url)

    raise RuntimeError(
        "FORESIGHT_DB_URL is not set.  Foresight requires a Postgres DSN. "
        "SQLite-as-primary is no longer supported.\n"
        "Example: export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'"
    )
