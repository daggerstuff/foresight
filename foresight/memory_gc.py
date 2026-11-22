"""Lightweight Memory GC Service.

Provides periodic garbage collection for memory system resources:
- Deletes memories past retention policy TTL (ephemeral >24h, short_term >7d)
- Prunes old memory_decay_events log (>30d)
- Prunes old maintenance events (>60d)
- Cleans orphaned entity links and embeddings (no FK cascade to protect them)

This is intentionally lightweight — no ML, no synthesis, just SQL batched deletes.
Designed to run frequently alongside heavy maintenance operations.
"""

import logging
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import errors as psycopg_errors

from .config import DB_PATH, DB_URL, raw_retention_window_days
from .connection_pool import get_pool
from .dependent_cleanup import purge_dead_synthesized_from_ids
from .retention_policy import apply_expiry_policies
from .sql_helpers import join_sql_parts

logger = logging.getLogger("foresight_memory_gc")

# One complete statement per dependent table and per self-referencing column:
# the identifier is never interpolated, so it cannot carry anything unexpected.
_ORPHAN_DEPENDENT_SQL = {
    "memory_decay_events": "DELETE FROM memory_decay_events WHERE memory_id NOT IN (SELECT id FROM memories)",
    "memory_versions": "DELETE FROM memory_versions WHERE memory_id NOT IN (SELECT id FROM memories)",
}
_ORPHAN_COLUMN_SQL = {
    "related_memory_id": (
        "UPDATE memories SET related_memory_id = NULL WHERE related_memory_id IS NOT NULL "
        "AND related_memory_id NOT IN (SELECT id FROM memories)"
    ),
    "superseded_by": (
        "UPDATE memories SET superseded_by = NULL WHERE superseded_by IS NOT NULL "
        "AND superseded_by NOT IN (SELECT id FROM memories)"
    ),
}

# Retention TTLs in hours
DEFAULT_RETENTION_TTLS: dict[str, int] = {
    "ephemeral": 24,
    "short_term": 168,  # 7 days
}

# Retention defaults for the event tables the collector prunes.
DEFAULT_DECAY_EVENTS_RETENTION_DAYS = 30
DEFAULT_MAINTENANCE_EVENTS_RETENTION_DAYS = 60
DEFAULT_MAX_BATCH_SIZE = 500


@dataclass
class GCConfig:
    """Configuration for memory garbage collection."""

    retention_ttls: dict[str, int] | None = None
    decay_events_retention_days: int = DEFAULT_DECAY_EVENTS_RETENTION_DAYS
    maintenance_events_retention_days: int = DEFAULT_MAINTENANCE_EVENTS_RETENTION_DAYS
    max_batch_size: int = DEFAULT_MAX_BATCH_SIZE
    raw_retention_window_days: int | None = field(default_factory=raw_retention_window_days)

    def to_dict(self) -> dict:
        return {
            "retention_ttls": dict(self.retention_ttls) if self.retention_ttls else dict(DEFAULT_RETENTION_TTLS),
            "decay_events_retention_days": self.decay_events_retention_days,
            "maintenance_events_retention_days": self.maintenance_events_retention_days,
            "max_batch_size": self.max_batch_size,
            "raw_retention_window_days": self.raw_retention_window_days,
        }


@dataclass
class GCStats:
    """Statistics from a GC run."""

    expired_memories_found: int = 0
    expired_memories_deleted: int = 0
    decay_events_pruned: int = 0
    maintenance_events_pruned: int = 0
    orphan_links_cleaned: int = 0
    orphan_embeddings_cleaned: int = 0
    orphan_dependents_cleaned: int = 0
    raw_events_tombstoned: int = 0
    policy_expired: int = 0
    expired_purged: int = 0
    gc_duration_seconds: float = 0.0
    bytes_freed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _GCRunContext:
    """The connection and per-run state shared by the GC phases."""

    conn: Any
    tenant_id: str
    cfg: GCConfig
    start_time: datetime
    stats: GCStats


def _purge_retention_expired(ctx: _GCRunContext, ttls: dict[str, int]) -> None:
    """Phase 1: delete memories past their retention TTL, in batches."""
    for retention, hours in ttls.items():
        cutoff = (ctx.start_time - timedelta(hours=hours)).isoformat()

        # Count found + estimate bytes
        cursor = ctx.conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0)
            FROM memories
            WHERE tenant_id = ? AND retention = ? AND created_at < ?
            """,
            (ctx.tenant_id, retention, cutoff),
        )
        row = cursor.fetchone()
        count_found = row[0] if row else 0
        bytes_est = row[1] if row else 0
        ctx.stats.expired_memories_found += count_found
        ctx.stats.bytes_freed += bytes_est

        if count_found == 0:
            continue

        deleted = 0
        while deleted < count_found:
            cursor = ctx.conn.execute(
                """
                DELETE FROM memories WHERE id IN (
                    SELECT id FROM memories
                    WHERE tenant_id = ? AND retention = ? AND created_at < ?
                    LIMIT ?
                )
                """,
                (ctx.tenant_id, retention, cutoff, ctx.cfg.max_batch_size),
            )
            batch = cursor.rowcount
            deleted += batch
            if batch == 0:
                break

        ctx.stats.expired_memories_deleted += deleted

    ctx.conn.commit()


def _purge_explicit_expiry(ctx: _GCRunContext) -> None:
    """Phase 1.5: delete memories whose expires_at has passed (PIX-4721)."""
    expiry_cutoff = ctx.start_time.isoformat()
    cursor = ctx.conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0)
        FROM memories
        WHERE tenant_id = ? AND expires_at IS NOT NULL AND expires_at < ?
        """,
        (ctx.tenant_id, expiry_cutoff),
    )
    row = cursor.fetchone()
    exp_count = row[0] if row else 0
    exp_bytes = row[1] if row else 0
    ctx.stats.expired_memories_found += exp_count
    ctx.stats.bytes_freed += exp_bytes

    if exp_count:
        deleted = 0
        while deleted < exp_count:
            cursor = ctx.conn.execute(
                """
                DELETE FROM memories WHERE id IN (
                    SELECT id FROM memories
                    WHERE tenant_id = ? AND expires_at IS NOT NULL AND expires_at < ?
                    LIMIT ?
                )
                """,
                (ctx.tenant_id, expiry_cutoff, ctx.cfg.max_batch_size),
            )
            batch = cursor.rowcount
            deleted += batch
            if batch == 0:
                break

        ctx.stats.expired_memories_deleted += deleted

    # Mental models share the same expires_at sweep semantics —
    # expired reflections are dropped rather than served stale.
    try:
        ctx.conn.execute(
            "DELETE FROM mental_models WHERE tenant_id = ? AND expires_at IS NOT NULL AND expires_at < ?",
            (ctx.tenant_id, expiry_cutoff),
        )
        ctx.conn.commit()
    except (sqlite3.OperationalError, psycopg_errors.UndefinedTable):
        pass

    ctx.conn.commit()


def _apply_expiry_policies(ctx: _GCRunContext) -> None:
    """Phase 1.7: stamp policy-expired memories; purge per purge_expired_after.

    Reversible expiry only — ``purge_expired_after`` (default 'never') is the
    sole destructive path. A missing memory_policies table or expiry columns
    (older schemas) degrades to a no-op.
    """
    try:
        result = apply_expiry_policies(ctx.conn, ctx.tenant_id, ctx.start_time)
    except Exception as exc:
        logger.warning("retention policy pass skipped: %s", exc)
        return
    ctx.stats.policy_expired += result.get("expired", 0)
    ctx.stats.expired_purged += result.get("purged", 0)
    ctx.conn.commit()


def _prune_decay_events(ctx: _GCRunContext) -> None:
    """Phase 2: prune old memory_decay_events."""
    decay_cutoff = (ctx.start_time - timedelta(days=ctx.cfg.decay_events_retention_days)).isoformat()
    cursor = ctx.conn.execute(
        """
        DELETE FROM memory_decay_events
        WHERE created_at < ? AND tenant_id = ?
        """,
        (decay_cutoff, ctx.tenant_id),
    )
    ctx.stats.decay_events_pruned = cursor.rowcount
    ctx.conn.commit()


def _prune_maintenance_events(ctx: _GCRunContext) -> None:
    """Phase 3: prune old maintenance events."""
    maint_cutoff = (ctx.start_time - timedelta(days=ctx.cfg.maintenance_events_retention_days)).isoformat()
    cursor = ctx.conn.execute(
        """
        DELETE FROM events
        WHERE event_type LIKE ? AND timestamp < ? AND tenant_id = ?
        """,
        ("maintenance%", maint_cutoff, ctx.tenant_id),
    )
    ctx.stats.maintenance_events_pruned = cursor.rowcount
    ctx.conn.commit()


def _prune_merge_history(ctx: _GCRunContext) -> None:
    """Phase 3.5: prune old memory_merge_history rows (maintenance-events retention)."""
    try:
        merge_cutoff = (ctx.start_time - timedelta(days=ctx.cfg.maintenance_events_retention_days)).isoformat()
        ctx.conn.execute(
            """
            DELETE FROM memory_merge_history
            WHERE merged_at < ? AND tenant_id = ?
            """,
            (merge_cutoff, ctx.tenant_id),
        )
        ctx.conn.commit()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            logging.getLogger(__name__).warning("memory_merge_history table not found, skipping prune: %s", e)
        else:
            raise
    except psycopg_errors.UndefinedTable as e:
        logging.getLogger(__name__).warning("memory_merge_history table not found, skipping prune: %s", e)


def _tombstone_raw_events(ctx: _GCRunContext) -> None:
    """Phase 5: tombstone raw events past the retention window."""
    if not ctx.cfg.raw_retention_window_days:
        return
    raw_cutoff = (ctx.start_time - timedelta(days=ctx.cfg.raw_retention_window_days)).isoformat()
    try:
        cursor = ctx.conn.execute(
            """
            UPDATE raw_events
            SET tombstone = 1, content = NULL, metadata = '{}'
            WHERE tenant_id = ? AND tombstone = 0 AND captured_at < ?
            """,
            (ctx.tenant_id, raw_cutoff),
        )
        ctx.conn.commit()
        ctx.stats.raw_events_tombstoned = cursor.rowcount
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            logging.getLogger(__name__).warning("raw_events table not found, skipping tombstone: %s", e)
        else:
            raise
    except psycopg_errors.UndefinedTable as e:
        logging.getLogger(__name__).warning("raw_events table not found, skipping tombstone: %s", e)


def _clean_orphans(ctx: _GCRunContext) -> None:
    """Phase 4: clean orphaned data (there is no FK cascade protection)."""
    # 4a: Orphaned memory_entity_links
    cursor = ctx.conn.execute(
        """
        DELETE FROM memory_entity_links
        WHERE memory_id NOT IN (SELECT id FROM memories)
        """,
    )
    ctx.stats.orphan_links_cleaned = cursor.rowcount
    ctx.conn.commit()

    # 4b: Orphaned memory_embeddings
    cursor = ctx.conn.execute(
        """
        DELETE FROM memory_embeddings
        WHERE memory_id NOT IN (SELECT id FROM memories)
        """,
    )
    ctx.stats.orphan_embeddings_cleaned = cursor.rowcount
    ctx.conn.commit()

    # 4c: Remaining dependents — decay events, versions, relationships,
    # chunk memory pointers, and intra-memories self-references.
    for orphan_sql in _ORPHAN_DEPENDENT_SQL.values():
        try:
            cursor = ctx.conn.execute(orphan_sql)
            ctx.stats.orphan_dependents_cleaned += cursor.rowcount or 0
        except (sqlite3.OperationalError, psycopg_errors.UndefinedTable):
            pass
    try:
        cursor = ctx.conn.execute(
            """
            DELETE FROM memory_relationships
            WHERE source_memory_id NOT IN (SELECT id FROM memories)
               OR target_memory_id NOT IN (SELECT id FROM memories)
            """,
        )
        ctx.stats.orphan_dependents_cleaned += cursor.rowcount or 0
        ctx.conn.execute(
            "UPDATE document_chunks SET memory_id = NULL "
            "WHERE memory_id IS NOT NULL AND memory_id NOT IN (SELECT id FROM memories)"
        )
        for clear_sql in _ORPHAN_COLUMN_SQL.values():
            ctx.conn.execute(clear_sql)
        try:
            ctx.stats.orphan_dependents_cleaned += purge_dead_synthesized_from_ids(ctx.conn) or 0
        except Exception as e:
            logger.debug(f"synthesized_from sweep skipped: {e}")
        ctx.conn.commit()
    except (sqlite3.OperationalError, psycopg_errors.UndefinedTable):
        pass


class MemoryGC:
    """
    Lightweight memory garbage collector.

    Performs cheap, frequent cleanup operations:
    1. Delete memories past retention TTL (ephemeral >24h, short_term >7d)
    2. Prune old memory_decay_events log
    3. Prune old maintenance events (>60d)
    4. Prune old memory_merge_history rows
    5. Tombstone raw events past retention window (content nulled, row kept)
    6. Clean orphaned memory_entity_links and memory_embeddings
    """

    def __init__(self, db_path: str):
        self.db_path = db_path

    def run(
        self,
        tenant_id: str = "default",
        config: GCConfig | None = None,
    ) -> GCStats:
        """Run garbage collection across all phases.

        Args:
            tenant_id: Tenant ID for scoped cleanup
            config: GC configuration (uses defaults if None)

        Returns:
            GCStats with per-phase deletion counts
        """
        start_time = datetime.now(UTC)
        cfg = config or GCConfig()
        ttls = cfg.retention_ttls if cfg.retention_ttls is not None else DEFAULT_RETENTION_TTLS
        stats = GCStats()

        pool = get_pool(self.db_path)
        conn = pool.acquire()

        try:
            ctx = _GCRunContext(
                conn=conn,
                tenant_id=tenant_id,
                cfg=cfg,
                start_time=start_time,
                stats=stats,
            )
            _purge_retention_expired(ctx, ttls)
            _purge_explicit_expiry(ctx)
            _apply_expiry_policies(ctx)
            _prune_decay_events(ctx)
            _prune_maintenance_events(ctx)
            _prune_merge_history(ctx)
            _tombstone_raw_events(ctx)
            _clean_orphans(ctx)
        finally:
            pool.release(conn)

        stats.gc_duration_seconds = (datetime.now(UTC) - start_time).total_seconds()

        logger.info(
            f"GC complete: deleted {stats.expired_memories_deleted}/{stats.expired_memories_found} "
            f"expired memories (~{stats.bytes_freed} bytes), "
            f"pruned {stats.decay_events_pruned} decay events, "
            f"{stats.maintenance_events_pruned} maintenance events, "
            f"{stats.orphan_links_cleaned} orphan links, "
            f"{stats.orphan_embeddings_cleaned} orphan embeddings, "
            f"{stats.orphan_dependents_cleaned} orphan dependents, "
            f"{stats.raw_events_tombstoned} raw events tombstoned "
            f"in {stats.gc_duration_seconds:.2f}s"
        )

        return stats


def tombstone_raw_events(
    user_id: str | None = None,
    session_id: str | None = None,
    older_than: str | None = None,
    tenant_id: str = "default",
    db_path: str | None = None,
) -> int:
    """Privacy-safe erasure of raw events: set tombstone=1 and clear content.

    The rows are retained for auditability but their sensitive content is
    nulled.  Filters combine with AND; at least user_id or session_id or
    older_than must be supplied to avoid accidental broad erasure.

    Args:
        user_id: Optional user scope.
        session_id: Optional session scope.
        older_than: Optional ISO-8601 cutoff; rows with captured_at < this are
            tombstoned (within the other filters).
        tenant_id: Tenant scope.
        db_path: Optional DB override.

    Returns:
        Number of raw events tombstoned.
    """
    if not user_id and not session_id and not older_than:
        raise ValueError("At least one of user_id, session_id, or older_than is required")

    pool = get_pool(db_path or DB_PATH or DB_URL)
    conn = pool.acquire()

    clauses = ["tenant_id = ?", "tombstone = 0"]
    params: list[str | int] = [tenant_id]

    if user_id:
        clauses.append("user_id = ?")
        params.append(user_id)
    if session_id:
        clauses.append("session_id = ?")
        params.append(session_id)
    if older_than:
        clauses.append("captured_at < ?")
        params.append(older_than)

    sql = join_sql_parts(
        "UPDATE raw_events SET tombstone = 1, content = NULL, metadata = '{}' WHERE ",
        " AND ".join(clauses),
    )

    try:
        cursor = conn.execute(sql, tuple(params))
        conn.commit()
        return cursor.rowcount
    finally:
        pool.release(conn)


# ---------------------------------------------------------------------------
# Singleton pattern (following ghost_cleanup.py)
# ---------------------------------------------------------------------------


class _MemoryGCSingleton:
    """Module-level singleton for MemoryGC."""

    _instance: MemoryGC | None = None
    _lock = __import__("threading").Lock()

    @classmethod
    def get_instance(cls, db_path: str | None = None) -> MemoryGC:
        """Get or create global memory GC instance."""
        with cls._lock:
            if cls._instance is None:
                if db_path is None:
                    db_path = DB_PATH or DB_URL
                if db_path is None:
                    raise RuntimeError("db_path or DB_PATH/DB_URL must be configured")
                cls._instance = MemoryGC(db_path)
            return cls._instance


def get_memory_gc(db_path: str | None = None) -> MemoryGC:
    """Get or create global memory GC instance."""
    return _MemoryGCSingleton.get_instance(db_path)


def run_memory_gc(tenant_id: str = "default", config: GCConfig | None = None) -> GCStats:
    """Run memory GC with default settings."""
    return get_memory_gc().run(tenant_id, config)
