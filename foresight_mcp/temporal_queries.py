"""Temporal Query Patterns for Time-Based Memory Retrieval.

Implements:
- Time-window retrieval (today/week/month/year)
- Time-weighted vector search
- Historical state queries
- Trend analysis
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from .config import DB_PATH
from .connection_pool import get_pool
from .tenant_context import get_current_tenant_id

if True:  # TYPE_CHECKING-compatible import guard for backend protocol
    from .backend.base import DatabaseBackend

logger = logging.getLogger("foresight_temporal_queries")


def _is_missing_tenant_column_error(error: Exception) -> bool:
    message = str(error).lower()
    return "no such column: tenant_id" in message or "no column named tenant_id" in message


TimeWindow = Literal["today", "week", "month", "year"]


@dataclass
class TemporalQueryResult:
    """Result of a temporal query."""

    memory_id: str
    content: str
    importance: float
    strength_trend: str
    activation_count: int
    created_at: str
    accessed_at: str
    category: str | None
    time_score: float = 0.0  # Recency score (0-1)
    combined_score: float = 0.0  # Vector + time combined


class TemporalQueryBuilder:
    """
    Builder for temporal memory queries.

    Provides fluent interface for time-based memory retrieval.
    Supports both direct SQLite (via ``db_path``) and any
    ``DatabaseBackend`` for backend-agnostic access.
    """

    def __init__(self, db_path: str, backend: DatabaseBackend | None = None):
        """Initialize query builder.

        Parameters
        ----------
        db_path :
            Path to SQLite database (used when ``backend`` is ``None``).
        backend :
            Optional backend instance.  When set, all queries route through
            ``backend.fetch()`` instead of the SQLite pool.
        """
        self.db_path = db_path
        self._backend = backend

    def _fetch_rows(self, sql: str, params: tuple | list = ()) -> list[dict]:
        """Execute a read query and return rows as dicts.

        Routes through the backend when available, otherwise falls back to the
        SQLite connection pool (with ``PRAGMA journal_mode=WAL``).
        """
        if self._backend is not None:
            return self._backend.fetch(sql, tuple(params) if isinstance(params, list) else params)
        pool = get_pool(self.db_path)
        conn = pool.acquire()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            pool.release(conn)

    def _get_window_hours(self, window: TimeWindow) -> int:
        """Get hours for time window."""
        return {
            "today": 24,
            "week": 168,
            "month": 720,
            "year": 8760,
        }[window]

    def get_memories_from_window(
        self,
        user_id: str,
        window: TimeWindow,
        *,
        limit: int = 50,
        min_importance: float = 0.1,
        category: str | None = None,
    ) -> list[TemporalQueryResult]:
        """Get memories from a time window."""
        tenant_id = get_current_tenant_id()
        window_hours = self._get_window_hours(window)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

        category_clause = "AND category = ?" if category else ""
        base_params: list = [user_id, tenant_id, cutoff.isoformat(), min_importance]
        if category:
            base_params.append(category)
        base_params.append(limit)

        sql = f"""
            SELECT
                id, content, importance, strength_trend,
                activation_count, created_at, accessed_at, category
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND created_at >= ?
            AND importance >= ?
            {category_clause}
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
        """
        try:
            rows = self._fetch_rows(sql, base_params)
        except Exception as e:
            if self._backend is not None:
                raise
            if not _is_missing_tenant_column_error(e):
                raise
            params: list = [user_id, cutoff.isoformat(), min_importance]
            if category:
                params.append(category)
            params.append(limit)
            sql_no_tenant = f"""
                SELECT
                    id, content, importance, strength_trend,
                    activation_count, created_at, accessed_at, category
                FROM memories
                WHERE user_id = ?
                AND created_at >= ?
                AND importance >= ?
                {category_clause}
                ORDER BY importance DESC, created_at DESC
                LIMIT ?
            """
            rows = self._fetch_rows(sql_no_tenant, params)

        return [
            TemporalQueryResult(
                memory_id=row["id"],
                content=row["content"],
                importance=row["importance"],
                strength_trend=row["strength_trend"],
                activation_count=row["activation_count"],
                created_at=row["created_at"],
                accessed_at=row["accessed_at"],
                category=row["category"],
            )
            for row in rows
        ]

    def get_memories_as_of_time(
        self, user_id: str, target_date: datetime, category: str | None = None, min_importance: float = 0.1
    ) -> list[TemporalQueryResult]:
        """Get memories as of a specific time."""
        tenant_id = get_current_tenant_id()
        category_clause = "AND category = ?" if category else ""
        base_params: list = [user_id, tenant_id, target_date.isoformat(), min_importance]
        if category:
            base_params = [user_id, tenant_id, category, target_date.isoformat(), min_importance]

        sql = f"""
            SELECT
                id, content, importance, strength_trend,
                activation_count, created_at, accessed_at, category
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND created_at <= ?
            AND importance > ?
            {category_clause}
            ORDER BY created_at DESC
        """
        try:
            rows = self._fetch_rows(sql, base_params)
        except Exception as e:
            if self._backend is not None:
                raise
            if not _is_missing_tenant_column_error(e):
                raise
            params: list = [user_id, target_date.isoformat(), min_importance]
            if category:
                params = [user_id, category, target_date.isoformat(), min_importance]
            sql_no_tenant = f"""
                SELECT
                    id, content, importance, strength_trend,
                    activation_count, created_at, accessed_at, category
                FROM memories
                WHERE user_id = ?
                AND created_at <= ?
                AND importance > ?
                {category_clause}
                ORDER BY created_at DESC
            """
            rows = self._fetch_rows(sql_no_tenant, params)

        return [
            TemporalQueryResult(
                memory_id=row["id"],
                content=row["content"],
                importance=row["importance"],
                strength_trend=row["strength_trend"],
                activation_count=row["activation_count"],
                created_at=row["created_at"],
                accessed_at=row["accessed_at"],
                category=row["category"],
            )
            for row in rows
        ]

    def get_memories_by_trend(
        self, user_id: str, trend: str, limit: int = 50, category: str | None = None
    ) -> list[TemporalQueryResult]:
        """Get memories by trend."""
        tenant_id = get_current_tenant_id()
        category_clause = "AND category = ?" if category else ""
        base_params: list = [user_id, tenant_id, trend, limit]
        if category:
            base_params = [user_id, tenant_id, category, trend, limit]

        sql = f"""
            SELECT
                id, content, importance, strength_trend,
                activation_count, created_at, accessed_at, category
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND strength_trend = ?
            {category_clause}
            ORDER BY created_at DESC
            LIMIT ?
        """
        try:
            rows = self._fetch_rows(sql, base_params)
        except Exception as e:
            if self._backend is not None:
                raise
            if not _is_missing_tenant_column_error(e):
                raise
            params: list = [user_id, trend, limit]
            if category:
                params = [user_id, category, trend, limit]
            sql_no_tenant = f"""
                SELECT
                    id, content, importance, strength_trend,
                    activation_count, created_at, accessed_at, category
                FROM memories
                WHERE user_id = ?
                AND strength_trend = ?
                {category_clause}
                ORDER BY created_at DESC
                LIMIT ?
            """
            rows = self._fetch_rows(sql_no_tenant, params)

        return [
            TemporalQueryResult(
                memory_id=row["id"],
                content=row["content"],
                importance=row["importance"],
                strength_trend=row["strength_trend"],
                activation_count=row["activation_count"],
                created_at=row["created_at"],
                accessed_at=row["accessed_at"],
                category=row["category"],
            )
            for row in rows
        ]

    def analyze_trends(self, user_id: str, timeframe: str = "30 days") -> dict:
        """Analyze memory trends over time."""
        tenant_id = get_current_tenant_id()

        # Daily stats
        daily_sql = """
            SELECT
                strftime('%Y-%m-%d', created_at) as date,
                COUNT(*) as count,
                AVG(importance) as avg_importance,
                SUM(CASE WHEN strength_trend = 'strengthening' THEN 1 ELSE 0 END) as strengthening,
                SUM(CASE WHEN strength_trend = 'stale' THEN 1 ELSE 0 END) as stale
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND created_at >= datetime('now', '-' || ?)
            GROUP BY date
            ORDER BY date
        """
        try:
            daily_rows = self._fetch_rows(daily_sql, (user_id, tenant_id, timeframe))
        except Exception as e:
            if self._backend is not None:
                raise
            if not _is_missing_tenant_column_error(e):
                raise
            daily_no_tenant = """
                SELECT
                    strftime('%Y-%m-%d', created_at) as date,
                    COUNT(*) as count,
                    AVG(importance) as avg_importance,
                    SUM(CASE WHEN strength_trend = 'strengthening' THEN 1 ELSE 0 END) as strengthening,
                    SUM(CASE WHEN strength_trend = 'stale' THEN 1 ELSE 0 END) as stale
                FROM memories
                WHERE user_id = ?
                AND created_at >= datetime('now', '-' || ?)
                GROUP BY date
                ORDER BY date
            """
            daily_rows = self._fetch_rows(daily_no_tenant, (user_id, timeframe))

        daily_stats = [
            {
                "date": row["date"],
                "count": row["count"],
                "avg_importance": row["avg_importance"] or 0,
                "strengthening": row["strengthening"] or 0,
                "stale": row["stale"] or 0,
            }
            for row in daily_rows
        ]

        # Category breakdown
        cat_sql = """
            SELECT
                COALESCE(category, 'general') as category,
                COUNT(*) as count,
                AVG(importance) as avg_importance,
                SUM(activation_count) as total_activations
            FROM memories
            WHERE user_id = ? AND tenant_id = ?
            AND created_at >= datetime('now', '-' || ?)
            GROUP BY category
            ORDER BY count DESC
        """
        try:
            cat_rows = self._fetch_rows(cat_sql, (user_id, tenant_id, timeframe))
        except Exception as e:
            if self._backend is not None:
                raise
            if not _is_missing_tenant_column_error(e):
                raise
            cat_no_tenant = """
                SELECT
                    COALESCE(category, 'general') as category,
                    COUNT(*) as count,
                    AVG(importance) as avg_importance,
                    SUM(activation_count) as total_activations
                FROM memories
                WHERE user_id = ?
                AND created_at >= datetime('now', '-' || ?)
                GROUP BY category
                ORDER BY count DESC
            """
            cat_rows = self._fetch_rows(cat_no_tenant, (user_id, timeframe))

        category_breakdown = [
            {
                "category": row["category"],
                "count": row["count"],
                "avg_importance": row["avg_importance"] or 0,
                "total_activations": row["total_activations"] or 0,
            }
            for row in cat_rows
        ]

        return {
            "daily_stats": daily_stats,
            "category_breakdown": category_breakdown,
            "overall_trend": self._calculate_overall_trend(daily_stats),
        }

    def _calculate_overall_trend(self, daily_stats: list[dict]) -> str:
        """Calculate overall trend from daily stats."""
        if len(daily_stats) < 3:
            return "insufficient_data"

        # Simple trend: compare first half avg to second half avg
        mid = len(daily_stats) // 2
        first_half = [d["avg_importance"] for d in daily_stats[:mid]]
        second_half = [d["avg_importance"] for d in daily_stats[mid:]]

        first_avg = sum(first_half) / len(first_half)
        second_avg = sum(second_half) / len(second_half)

        delta = second_avg - first_avg

        if delta > 0.1:
            return "improving"
        if delta < -0.1:
            return "declining"
        return "stable"

    def get_time_weighted_scores(self, memory_ids: list[str], user_id: str) -> dict:
        """Calculate time-weighted scores for memories."""
        if not memory_ids:
            return {}
        tenant_id = get_current_tenant_id()
        placeholders = ",".join("?" * len(memory_ids))
        rows = self._fetch_rows(
            f"""
                SELECT id, created_at, activation_count
                FROM memories
                WHERE id IN ({placeholders}) AND user_id = ? AND tenant_id = ?
            """,
            [*memory_ids, user_id, tenant_id],
        )

        scores = {}
        now = datetime.now(timezone.utc)
        for row in rows:
            memory_id = row["id"]
            created_at_str = row["created_at"]
            activation_count = row["activation_count"] or 0
            created = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            hours_old = (now - created).total_seconds() / 3600

            time_score = pow(0.5, hours_old / 168)
            activation_boost = 1 + (activation_count * 0.05)
            time_score = min(1.0, time_score * activation_boost)
            scores[memory_id] = time_score

        return scores


# Global instance management (thread-safe) using state dictionary
_MODULE_STATE: dict[str, Any] = {
    "temporal_query_builder": None,
}
_temporal_query_lock = threading.Lock()


def get_temporal_query_builder(
    db_path: str | None = None,
    backend: DatabaseBackend | None = None,
) -> TemporalQueryBuilder:
    """Get or create global temporal query builder instance (thread-safe)."""
    with _temporal_query_lock:
        if _MODULE_STATE["temporal_query_builder"] is None:
            if db_path is None:
                db_path = DB_PATH
            _MODULE_STATE["temporal_query_builder"] = TemporalQueryBuilder(db_path, backend=backend)
    return _MODULE_STATE["temporal_query_builder"]


def reset_temporal_query_builder() -> None:
    """Reset global temporal query builder (for testing)."""
    with _temporal_query_lock:
        _MODULE_STATE["temporal_query_builder"] = None
