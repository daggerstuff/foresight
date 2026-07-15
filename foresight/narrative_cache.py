"""
Postgres-backed cache for generated reflection narratives.

The cache stores LLM-derived narrative text keyed by tenant, user, report,
model version, and insights hash. Uses the shared ``narrative_cache`` table
created by schema migrations.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

DEFAULT_MAX_ENTRIES = 10_000
DEFAULT_TTL_SECONDS = 604_800


class NarrativeCache:
    """Persistent Postgres cache with tenant/user isolation and LRU eviction."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0")

        self.max_entries = max_entries
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._eviction_count = 0
        self._closed = False

    def _get_conn(self):
        """Acquire a Postgres connection via get_db_connection()."""
        from .server import get_db_connection

        return get_db_connection()

    def get(
        self,
        report_id: str,
        *,
        tenant_id: str,
        user_id: str,
        model_version: str,
        insights_hash: str,
    ) -> str | None:
        """Return a cached narrative, or ``None`` on miss or TTL expiry."""
        self._validate_parts(
            report_id=report_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        cache_key = self._cache_key(
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=report_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        now = time.time()

        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    """
                    SELECT narrative, created_at
                    FROM narrative_cache
                    WHERE cache_key = %s AND tenant_id = %s AND user_id = %s
                    """,
                    (cache_key, tenant_id, user_id),
                ).fetchone()

                if row is None:
                    self._misses += 1
                    return None

                if row["created_at"] < now - self.ttl_seconds:
                    conn.execute(
                        """
                        DELETE FROM narrative_cache
                        WHERE cache_key = %s AND tenant_id = %s AND user_id = %s
                        """,
                        (cache_key, tenant_id, user_id),
                    )
                    conn.commit()
                    self._misses += 1
                    return None

                conn.execute(
                    """
                    UPDATE narrative_cache
                    SET last_accessed_at = %s, access_count = access_count + 1
                    WHERE cache_key = %s AND tenant_id = %s AND user_id = %s
                    """,
                    (now, cache_key, tenant_id, user_id),
                )
                conn.commit()
                self._hits += 1
                return str(row["narrative"])
            finally:
                conn.close()

    def put(
        self,
        report_id: str,
        narrative: str,
        *,
        tenant_id: str,
        user_id: str,
        model_version: str,
        insights_hash: str,
    ) -> None:
        """Insert or replace a cached narrative and enforce size bounds."""
        self._validate_parts(
            report_id=report_id,
            tenant_id=tenant_id,
            user_id=user_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        if not isinstance(narrative, str):
            raise TypeError("narrative must be a string")

        cache_key = self._cache_key(
            tenant_id=tenant_id,
            user_id=user_id,
            report_id=report_id,
            model_version=model_version,
            insights_hash=insights_hash,
        )
        now = time.time()

        with self._lock:
            conn = self._get_conn()
            try:
                if self._size(conn) >= int(self.max_entries * 0.9):
                    self._delete_expired(conn, now)

                conn.execute(
                    """
                    INSERT INTO narrative_cache (
                        cache_key,
                        tenant_id,
                        user_id,
                        report_id,
                        model_version,
                        insights_hash,
                        narrative,
                        created_at,
                        last_accessed_at,
                        access_count
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        tenant_id = EXCLUDED.tenant_id,
                        user_id = EXCLUDED.user_id,
                        report_id = EXCLUDED.report_id,
                        model_version = EXCLUDED.model_version,
                        insights_hash = EXCLUDED.insights_hash,
                        narrative = EXCLUDED.narrative,
                        created_at = EXCLUDED.created_at,
                        last_accessed_at = EXCLUDED.last_accessed_at,
                        access_count = 0
                    """,
                    (
                        cache_key,
                        tenant_id,
                        user_id,
                        report_id,
                        model_version,
                        insights_hash,
                        narrative,
                        now,
                        now,
                    ),
                )
                conn.commit()
                self._evict_lru(conn)
            finally:
                conn.close()

    def clear(self, tenant_id: str | None = None) -> int:
        """Clear all cache entries, or only entries for one tenant."""
        with self._lock:
            conn = self._get_conn()
            try:
                if tenant_id is None:
                    cursor = conn.execute("DELETE FROM narrative_cache")
                else:
                    cursor = conn.execute(
                        "DELETE FROM narrative_cache WHERE tenant_id = %s",
                        (tenant_id,),
                    )
                conn.commit()
                rowcount = int(cursor.rowcount)
            finally:
                conn.close()
            return rowcount

    def stats(self) -> dict[str, Any]:
        """Return cache size and in-process hit/eviction counters."""
        with self._lock:
            conn = self._get_conn()
            try:
                size = self._size(conn)
            finally:
                conn.close()
            requests = self._hits + self._misses
            hit_rate = self._hits / requests if requests else 0.0
            return {
                "size": size,
                "max_entries": self.max_entries,
                "ttl_seconds": self.ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
                "eviction_count": self._eviction_count,
            }

    def close(self) -> None:
        """Mark the cache as closed. No persistent connection to close."""
        with self._lock:
            self._closed = True

    def _size(self, conn) -> int:
        # Use positional access so the row factory (index or dict) does not
        # matter; works for both Postgres RealDict and sqlite3.Row tuples.
        row = conn.execute("SELECT COUNT(*) FROM narrative_cache").fetchone()
        return int(row[0])

    def _delete_expired(self, conn, now: float) -> None:
        cursor = conn.execute(
            "DELETE FROM narrative_cache WHERE created_at < %s",
            (now - self.ttl_seconds,),
        )
        self._eviction_count += max(int(cursor.rowcount), 0)

    def _evict_lru(self, conn) -> None:
        overflow = self._size(conn) - self.max_entries
        if overflow <= 0:
            return

        cursor = conn.execute(
            """
            DELETE FROM narrative_cache
            WHERE cache_key IN (
                SELECT cache_key
                FROM narrative_cache
                ORDER BY last_accessed_at ASC, created_at ASC
                LIMIT %s
            )
            """,
            (overflow,),
        )
        self._eviction_count += max(int(cursor.rowcount), 0)

    @staticmethod
    def _cache_key(
        *,
        tenant_id: str,
        user_id: str,
        report_id: str,
        model_version: str,
        insights_hash: str,
    ) -> str:
        payload = json.dumps(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "report_id": report_id,
                "model_version": model_version,
                "insights_hash": insights_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_parts(
        *,
        report_id: str,
        tenant_id: str,
        user_id: str,
        model_version: str,
        insights_hash: str,
    ) -> None:
        values = {
            "report_id": report_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "model_version": model_version,
            "insights_hash": insights_hash,
        }
        for name, value in values.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} is required and must be a non-empty string")


__all__ = ["DEFAULT_MAX_ENTRIES", "DEFAULT_TTL_SECONDS", "NarrativeCache"]
