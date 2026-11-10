"""Durable storage for the built-in OAuth 2.1 authorization server.

``ForesightOAuthProvider`` (``auth_oauth.py``) keeps client registrations,
authorization codes, and access/refresh tokens here so that a server restart
does not strand already-authorized MCP clients (e.g. a cloud connector that
stored the ``client_id`` issued by an earlier ``/register`` call).

The store is the Postgres backend through the shared connection pool
(``connection_pool.get_pool``):

* Production (``FORESIGHT_DB_URL`` set, backend initialized) → the active
  Postgres pool.
* Tests (explicit file path) → a SQLite pool, mirroring how the rest of the
  auth layer is exercised (see ``tests/test_auth.py``).

``get_pool`` may raise ``RuntimeError`` before the backend is initialized
(e.g. at module import time, when the provider is constructed). Callers treat
a failed store operation as "memory-only" and continue, so OAuth never breaks
because persistence is unavailable.

All rows serialize whole pydantic objects (``model_dump_json``) rather than
normalizing field-by-field, so schema evolution of the SDK models cannot
silently drop data.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS oauth_clients ("
    "client_id TEXT PRIMARY KEY,"
    "client_info_json TEXT NOT NULL"
    ");"
    "CREATE TABLE IF NOT EXISTS oauth_auth_codes ("
    "code TEXT PRIMARY KEY,"
    "code_json TEXT NOT NULL,"
    "expires_at REAL NOT NULL"
    ");"
    "CREATE TABLE IF NOT EXISTS oauth_tokens ("
    "token TEXT PRIMARY KEY,"
    "kind TEXT NOT NULL,"
    "token_json TEXT NOT NULL,"
    "expires_at REAL"
    ");"
    "CREATE INDEX IF NOT EXISTS idx_oauth_tokens_kind ON oauth_tokens(kind);"
)


def ensure_schema(db_path: str | None) -> None:
    """Create the OAuth tables if they do not exist (idempotent)."""
    from foresight.connection_pool import get_pool

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        for raw_statement in _SCHEMA.split(";"):
            statement = raw_statement.strip()
            if statement:
                conn.execute(statement)
        conn.commit()
    finally:
        pool.release(conn)


def persist_client(db_path: str | None, client_id: str, client_json: str) -> None:
    """Insert or update a registered client."""
    from foresight.connection_pool import get_pool

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        conn.execute(
            "INSERT INTO oauth_clients (client_id, client_info_json) VALUES (?, ?)"
            " ON CONFLICT (client_id) DO UPDATE SET client_info_json = excluded.client_info_json",
            (client_id, client_json),
        )
        conn.commit()
    finally:
        pool.release(conn)


def load_client(db_path: str | None, client_id: str) -> str | None:
    """Return the stored client JSON for ``client_id``, or ``None``."""
    from foresight.connection_pool import get_pool

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        row = conn.execute(
            "SELECT client_info_json FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
        return row["client_info_json"] if row else None
    finally:
        pool.release(conn)


def persist_auth_code(db_path: str | None, code: str, code_json: str, expires_at: float) -> None:
    """Insert or update an authorization code."""
    from foresight.connection_pool import get_pool

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        conn.execute(
            "INSERT INTO oauth_auth_codes (code, code_json, expires_at) VALUES (?, ?, ?)"
            " ON CONFLICT (code) DO UPDATE SET code_json = excluded.code_json,"
            " expires_at = excluded.expires_at",
            (code, code_json, expires_at),
        )
        conn.commit()
    finally:
        pool.release(conn)


def load_auth_code(db_path: str | None, code: str) -> tuple[str, float] | None:
    """Return ``(code_json, expires_at)`` for a code, or ``None``."""
    from foresight.connection_pool import get_pool

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        row = conn.execute(
            "SELECT code_json, expires_at FROM oauth_auth_codes WHERE code = ?",
            (code,),
        ).fetchone()
        return (row["code_json"], row["expires_at"]) if row else None
    finally:
        pool.release(conn)


def delete_auth_code(db_path: str | None, code: str) -> None:
    """Delete an authorization code (single-use consumption)."""
    from foresight.connection_pool import get_pool

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        conn.execute("DELETE FROM oauth_auth_codes WHERE code = ?", (code,))
        conn.commit()
    finally:
        pool.release(conn)


def persist_token(db_path: str | None, token: str, kind: str, token_json: str, expires_at: float | None) -> None:
    """Insert or update an access or refresh token (``kind`` = "access"/"refresh")."""
    from foresight.connection_pool import get_pool

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        conn.execute(
            "INSERT INTO oauth_tokens (token, kind, token_json, expires_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (token) DO UPDATE SET kind = excluded.kind,"
            " token_json = excluded.token_json, expires_at = excluded.expires_at",
            (token, kind, token_json, expires_at),
        )
        conn.commit()
    finally:
        pool.release(conn)


def load_token(db_path: str | None, token: str) -> tuple[str, str, float | None] | None:
    """Return ``(kind, token_json, expires_at)`` for a token, or ``None``."""
    from foresight.connection_pool import get_pool

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        row = conn.execute(
            "SELECT kind, token_json, expires_at FROM oauth_tokens WHERE token = ?",
            (token,),
        ).fetchone()
        return (row["kind"], row["token_json"], row["expires_at"]) if row else None
    finally:
        pool.release(conn)


def delete_token(db_path: str | None, token: str) -> None:
    """Delete a token (single-use consumption or revocation)."""
    from foresight.connection_pool import get_pool

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        conn.execute("DELETE FROM oauth_tokens WHERE token = ?", (token,))
        conn.commit()
    finally:
        pool.release(conn)


def purge_expired(db_path: str | None) -> int:
    """Delete expired auth codes and tokens. Returns the number removed."""
    from foresight.connection_pool import get_pool

    now = time.time()
    pool = get_pool(db_path)
    conn = pool.acquire()
    removed = 0
    try:
        removed += conn.execute("DELETE FROM oauth_auth_codes WHERE expires_at < ?", (now,)).rowcount or 0
        removed += (
            conn.execute(
                "DELETE FROM oauth_tokens WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            ).rowcount
            or 0
        )
        conn.commit()
    finally:
        pool.release(conn)
    return removed
