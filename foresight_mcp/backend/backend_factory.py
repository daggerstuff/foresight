"""Backend factory — selects and instantiates the correct DatabaseBackend.

Reads ``FORESIGHT_DB_URL`` (or an explicit ``db_url`` argument) and returns
either a ``PostgresBackend`` or ``SqliteBackend``.

Usage::

    from foresight_mcp.backend.backend_factory import create_backend

    backend = create_backend()
    backend.connect()
    rows = backend.fetch("SELECT * FROM memories WHERE id = ?", ("abc",))
"""

from __future__ import annotations

import os

from ..config import DB_PATH, DB_URL
from .base import DatabaseBackend
from .postgres_backend import PostgresBackend
from .sqlite_backend import SqliteBackend


def create_backend(db_url: str | None = None) -> DatabaseBackend:
    """Create the appropriate database backend based on configuration.

    Parameters
    ----------
    db_url :
        Database URL.  If ``None`` (default) the environment variable
        ``FORESIGHT_DB_URL`` is read.  When neither is set the default
        SqliteBackend (at ``FORESIGHT_DB_PATH``) is returned.

    Returns
    -------
    DatabaseBackend
        An **unconnected** backend — call ``.connect()`` before use.
    """
    if db_url is None:
        # Read from the live environment so code and tests that
        # set ``FORESIGHT_DB_URL`` *after* import still work.
        db_url = os.environ.get("FORESIGHT_DB_URL", "") or ""

    if not db_url:
        # Fall back to the module-level constant (set at import time).
        db_url = DB_URL

    if db_url and db_url.startswith(("postgresql://", "postgres://")):
        return PostgresBackend(dsn=db_url)

    return SqliteBackend(db_path=DB_PATH)
