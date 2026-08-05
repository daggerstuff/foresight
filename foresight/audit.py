"""Tenant-isolated audit log for foresight clinical workflows.

Replaces the Python ``logging``-based stopgap (PIX-3738) with a
table that supports queryable, tenant-isolated, retention-controlled
audit events. Every event in the system that touches PHI or LLM-derived
output should emit a row through this module.

Append-only tamper-evidence
---------------------------

The ``audit_events`` table is append-only at the application layer —
this module exposes no ``UPDATE`` or ``DELETE`` methods.

Tenant isolation
----------------

Every :meth:`AuditLog.record` call requires ``tenant_id``; every
:meth:`AuditLog.query` call requires ``tenant_id`` as a positional
argument. Cross-tenant reads are not expressible. ``query()`` returns
an empty list for a tenant with no events rather than raising.

Backward compatibility
----------------------

If no :class:`AuditLog` is configured (e.g. unit tests, ephemeral CLI
runs), callers should fall back to ``logger.info(...)`` rather than
requiring a database. The narrative module wires this fallback
automatically via the optional ``audit_log`` parameter.

Storage
-------

Audit events live in the main database (Postgres in production, SQLite
in tests). The table and indexes are created by the server's schema
migrations; this module creates them lazily if needed.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .connection_pool import get_pool

logger = logging.getLogger("foresight_audit")

NARRATIVE_GENERATED = "narrative_generated"
NARRATIVE_FAILED = "narrative_failed"
NARRATIVE_CACHE_HIT = "narrative_cache_hit"
LLM_CALL_SUCCEEDED = "llm_call_succeeded"
LLM_CALL_FAILED = "llm_call_failed"

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        created_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_time ON audit_events(tenant_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_events_type ON audit_events(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_audit_events_resource ON audit_events(resource_id)",
    "CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_type ON audit_events(tenant_id, event_type)",
]


@dataclass(frozen=True)
class AuditEvent:
    """A single audit event to be persisted via :meth:`AuditLog.record`."""

    tenant_id: str
    user_id: str
    event_type: str
    resource_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.tenant_id or not isinstance(self.tenant_id, str):
            raise ValueError("tenant_id is required and must be a non-empty string")
        if not self.user_id or not isinstance(self.user_id, str):
            raise ValueError("user_id is required and must be a non-empty string")
        if not self.event_type or not isinstance(self.event_type, str):
            raise ValueError("event_type is required and must be a non-empty string")
        if not isinstance(self.resource_id, str):
            raise ValueError("resource_id must be a string (use '' if not applicable)")
        if not isinstance(self.metadata, dict):
            raise ValueError("metadata must be a dict")


class AuditLog:
    """Tenant-isolated audit log backed by the connection pool.

    The connection is acquired lazily on first use from the pool.
    The schema (table + indexes) is created on first use if needed.
    A single instance is safe to share across threads; the internal
    lock serializes writes.

    The instance registers an ``atexit`` handler that releases the
    underlying connection when the interpreter shuts down. Callers
    that want deterministic lifecycle (e.g. tests) should call
    :meth:`close` explicitly.
    """

    def __init__(self, db_path: str) -> None:
        if not db_path or not isinstance(db_path, str):
            raise ValueError("db_path is required and must be a non-empty string")
        self._db_path = db_path
        self._conn: Any | None = None
        self._lock = threading.Lock()
        self._closed = False
        atexit.register(self.close)

    def _get_conn(self) -> Any:
        if self._conn is None:
            pool = get_pool(self._db_path)
            conn = pool.acquire()
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception as exc:
                    logger.warning("audit close failed: %s", exc)
                self._conn = None
                self._closed = True

    def __enter__(self) -> AuditLog:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def record(self, event: AuditEvent) -> None:
        """Persist an audit event. Append-only — no UPDATE or DELETE exposed."""
        if self._closed:
            raise RuntimeError("AuditLog is closed")
        metadata_json = json.dumps(event.metadata, sort_keys=True, default=str)
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                INSERT INTO audit_events
                    (tenant_id, user_id, event_type, resource_id, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.tenant_id,
                    event.user_id,
                    event.event_type,
                    event.resource_id,
                    metadata_json,
                    event.created_at,
                ),
            )
            conn.commit()

    def query(
        self,
        tenant_id: str,
        *,
        since: float | None = None,
        until: float | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Query audit events for a single tenant."""
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("tenant_id is required and must be a non-empty string")
        if limit <= 0:
            raise ValueError("limit must be a positive integer")

        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        if until is not None:
            clauses.append("created_at <= ?")
            params.append(until)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)

        params.append(limit)
        sql = (
            "SELECT tenant_id, user_id, event_type, resource_id, metadata_json, created_at "
            "FROM audit_events "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at DESC, id DESC "
            "LIMIT ?"
        )

        conn = self._get_conn()
        rows = conn.execute(sql, params).fetchall()
        return [
            AuditEvent(
                tenant_id=row["tenant_id"],
                user_id=row["user_id"],
                event_type=row["event_type"],
                resource_id=row["resource_id"],
                metadata=json.loads(row["metadata_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def count(self, tenant_id: str, *, event_type: str | None = None) -> int:
        """Count audit events for a tenant (cheap, no row hydration)."""
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("tenant_id is required and must be a non-empty string")
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        sql = f"SELECT COUNT(*) AS n FROM audit_events WHERE {' AND '.join(clauses)}"
        conn = self._get_conn()
        return int(conn.execute(sql, params).fetchone()["n"])

    def stats(self, tenant_id: str) -> dict[str, Any]:
        """Aggregate stats for a tenant."""
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("tenant_id is required and must be a non-empty string")
        conn = self._get_conn()
        total_row = conn.execute(
            "SELECT COUNT(*) AS n, MIN(created_at) AS first_at, MAX(created_at) AS last_at "
            "FROM audit_events WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        type_rows = conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM audit_events WHERE tenant_id = ? GROUP BY event_type",
            (tenant_id,),
        ).fetchall()
        return {
            "total": int(total_row["n"]),
            "by_type": {row["event_type"]: int(row["n"]) for row in type_rows},
            "first_at": total_row["first_at"],
            "last_at": total_row["last_at"],
        }


__all__ = [
    "LLM_CALL_FAILED",
    "LLM_CALL_SUCCEEDED",
    "NARRATIVE_CACHE_HIT",
    "NARRATIVE_FAILED",
    "NARRATIVE_GENERATED",
    "AuditEvent",
    "AuditLog",
]
