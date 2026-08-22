#!/usr/bin/env python3
"""
Foresight MCP Server - Full memory system with psychological safety features.
Restored from src/lib/ai/memory/ architecture.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware as _Middleware
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import AuthMiddleware, get_auth_manager
from .backend import RedisCompanion, create_backend
from .backend.backend_migrations import ensure_schema_migrations_table
from .block_registry import InjectionPoint, initialize_default_blocks
from .capture import get_capture_pipeline
from .clustering import ClusterResult, cluster_memories
from .config import (
    BANK_ID,
    DB_PATH,
    DEFAULT_BURST_LIMIT,
    DEFAULT_RATE_LIMIT,
    DEFAULT_TENANT_ID,
    REDIS_URL,
    USER_ID,
)
from .connection_pool import PooledConnection, reset_pool
from .context_blocks import (
    PENDING_ITEMS,
    PROJECT_CONTEXT,
    SESSION_PATTERNS,
    USER_PREFERENCES,
    get_context_block_agent,
)
from .crisis_detection import get_crisis_service
from .decay_model import DecayConfigOptions, get_decay_model
from .document_layer import (
    DEFAULT_CHUNK_CHAR_BUDGET as _DOC_CHUNK_BUDGET,
    DocumentCreateOptions,
    DocumentLayerError,
    content_hash as _content_hash,
    get_document_store,
)
from .enhanced_synthesizer import get_enhanced_synthesizer
from .entity_extractor import Entity, get_entity_extractor
from .event_bus import (
    curation_status_changed,
    get_event_bus,
    memory_deleted,
    memory_retrieved,
    memory_stored,
    memory_updated,
    sync_progress,
)
from .graph_store import get_graph_store
from .hooks import (
    HookResult,
    MemoryHookContext,
    MemoryHookType,
    get_memory_hook_registry,
)
from .hybrid_retriever import HybridResult, HybridSearchOptions, HybridSearchResult, get_hybrid_retriever
from .injection_budget import (
    DEFAULT_LANE_WEIGHTS,
    MIN_LANE_CHARS,
    BudgetResult,
    InjectionBudget,
    Lane,
    LaneItem,
    format_budgeted_payload,
)
from .memory_components import (
    MemoryCrisisTagger,
    MemoryLinker,
    MemorySynthesizer,
    SocraticGate,
)
from .memory_maintenance import MaintenanceConfig, MemoryMaintenanceJob
from .memory_gc import run_memory_gc
from .graph_edge_decay import run_edge_decay_update, run_edge_pruning
from .ghost_cleanup import run_ghost_cleanup
from .memory_relationships import (
    LinkMemoriesOptions,
    MemoryRelationshipError,
    get_memory_relationship_store,
)
from .memory_types import (
    EmotionalMetadata,
    EmpathyMetrics,
    MemoryObject,
    MemoryScope,
    RetentionPolicy,
)
from .narrative_cache import NarrativeCache
from .phrase_triggers import DEFAULT_TRIGGERS, extract_triggered_memories
from .profile_synthesizer import ProfileConfig, profile_to_prompt, synthesize_profile as _synthesize_profile
from .rate_limiter import RateLimitExceededError, get_rate_limiter
from .llm_client import TenantLLMClient
from .llm_errors import LLMError, LLMNotConfiguredError
from .reflection_engine import get_reflection_engine
from .reflection_narrative import (
    ReflectionNarrativeError,
    _default_cache as _reflection_narrative_cache,
    generate_insight_narrative,
)
from .semantic_search import (
    DEFAULT_PROVIDER as _SEMANTIC_DEFAULT_PROVIDER,
    SemanticSearchError as _SemanticSearchError,
    SemanticSearchOptions,
    get_semantic_search,
)
from .sensitivity import resolve_is_sensitive
from .sync import Operation, OperationQueue, OperationType, get_sync_manager, SyncProgress
from .temporal_queries import get_temporal_query_builder
from .temporal_service import get_temporal_service
from .tenant_context import get_current_account_id, get_current_user_id, set_current_tenant_id
from .tenant_middleware import TenantMiddleware

# WebSocket imports
from .websocket.server import WebSocketServer

DEFAULT_MAX_MEMORY_PER_TENANT = 100_000
DEFAULT_MAX_CACHE_ENTRIES_PER_TENANT = 50_000
DEFAULT_MAX_TFIDF_CACHE_SIZE = 10_000

# Last injection tracking for system status visibility (PIX-3955)
_last_injection_stats: dict[str, Any] = {}

# Global database backend (set during main() startup, PIX-3994)
_global_backend: Any = None

# Global Redis companion (set during main() startup, PIX-3995)
_redis_companion: RedisCompanion | None = None


# Global narrative cache instance for metrics (lazy initialization)
class _NarrativeCacheSingleton:
    """Module-level singleton for NarrativeCache."""

    _instance: NarrativeCache | None = None

    @classmethod
    def get_instance(cls) -> NarrativeCache:
        """Get or create global narrative cache instance."""
        if cls._instance is None:
            cls._instance = NarrativeCache()
        return cls._instance


def get_narrative_cache() -> NarrativeCache:
    """Get or create global narrative cache instance."""
    return _NarrativeCacheSingleton.get_instance()


# Tool argument grouping models
class MemoryOptions(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    category: str = Field(default="fact", description="Category label")
    scope: str = Field(default="session", description="Memory scope: session, arc, trait, or fact")
    retention: str = Field(
        default="short_term", description="Retention policy: ephemeral, short_term, long_term, or permanent"
    )
    importance: float = Field(default=0.5, description="Initial importance score (0.0 to 1.0)")
    emotional_context: dict[str, Any] | None = Field(
        default=None, description="Emotional metadata (valence, arousal, dominance, primary_emotion, intensity)"
    )
    metrics: dict[str, Any] | None = Field(
        default=None, description="Empathy metrics (reciprocity, validation_accuracy, resistance_level)"
    )
    relation_type: str | None = Field(
        default=None,
        description="Optional typed relationship to another memory. Allowed: updates, extends, derives, contradicts, supports, related",
    )
    related_memory_id: str | None = Field(
        default=None, description="ID of the memory this one relates to (paired with relation_type)"
    )
    is_sensitive: bool | None = Field(
        default=None,
        description=(
            "PIX-3956 sensitivity override. None defers to the detector on memory.content;"
            " True forces the row is_sensitive=1; False forces is_sensitive=0."
        ),
    )


class MemoryUpdateOptions(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    content: str | None = Field(default=None, description="New memory content")
    category: str | None = Field(default=None, description="New category label")
    scope: str | None = Field(default=None, description="New memory scope")
    retention: str | None = Field(default=None, description="New retention policy")
    tags: list[str] | None = Field(default=None, description="New list of tags")
    is_sensitive: bool | None = Field(
        default=None,
        description=(
            "Sensitivity override on update. None defers to the detector on memory.content;"
            " True forces is_sensitive=1; False forces is_sensitive=0."
        ),
    )
    relation_type: str | None = Field(
        default=None,
        description="Optional typed relationship to another memory. Allowed: updates, extends, derives, contradicts, supports, related",
    )
    related_memory_id: str | None = Field(
        default=None, description="ID of the memory this one relates to (paired with relation_type)"
    )


class SearchOptions(BaseModel):
    query_type: Literal["id", "keyword", "list", "semantic"] = Field(
        default="keyword", description="Type of search/retrieval"
    )
    query: str | None = Field(default=None, description="Search query string")
    memory_id: str | None = Field(default=None, description="Retrieve specific memory by ID")
    limit: int = Field(default=10, description="Maximum results")
    offset: int = Field(default=0, description="Result offset")
    min_importance: float = Field(default=0.1, description="Minimum importance threshold")
    use_hybrid: bool = Field(default=True, description="Enable hybrid search signals")
    use_cascade: bool = Field(default=False, description="Enable cascade search across related entities")
    cascade_depth: int = Field(default=2, description="Maximum depth for cascade traversal")
    cascade_limit: int = Field(default=100, description="Maximum results per cascade level")
    debug: bool = Field(
        default=False,
        description="Return structured trace with timing and signal metadata instead of formatted results",
    )
    min_score: float = Field(default=0.0, description="Minimum cosine similarity for semantic search [-1.0, 1.0]")
    provider: str | None = Field(default=None, description="Embedder provider name for semantic search")


class ContextBlockAction(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    action: Literal["list", "get", "update", "reset", "clear"] = Field(description="Action to perform")
    label: str | None = Field(default=None, description="Block label (e.g. guidance, preferences, project_context, pending_items)")
    content: str | None = Field(default=None, description="New content for update action")

    @model_validator(mode="before")
    @classmethod
    def handle_label_aliases(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if not values.get("label"):
                for alias in ("block_name", "block_type", "name", "block", "block_id", "key"):
                    if values.get(alias):
                        values["label"] = values[alias]
                        break
        return values


class SubconsciousAction(ContextBlockAction):
    """Compatibility alias for the older subconscious-named tool contract."""


class CurationRunAction(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    action: Literal["create", "get", "list", "cancel", "archive"] = Field(description="Action to perform")
    run_id: str | None = Field(default=None, description="Curation run ID for get/cancel/archive")
    source_bank_id: str | None = Field(default=None, description="Source bank to curate from")
    output_bank_id: str | None = Field(default=None, description="Optional output bank for reviewable results")
    policy_mode: Literal["preserve", "rebalance", "rebuild"] = Field(
        default="rebalance", description="Curation policy mode"
    )
    tool_access: Literal["disabled", "observe", "operate"] = Field(
        default="observe", description="Curator tool-access policy"
    )
    output_mode: Literal["reviewable_output", "in_place"] = Field(
        default="reviewable_output",
        description="Whether curated results land in a reviewable output bank or in place",
    )
    instructions: str | None = Field(default=None, description="Optional curator instructions")
    run_clustering: bool = Field(
        default=False,
        description="If True, run memory clustering after curation completes",
    )
    transcript_bundle: list[dict[str, Any]] | None = Field(
        default=None, description="Optional transcript bundle to incorporate during curation"
    )
    session_id: str | None = Field(default=None, description="Optional session ID for transcript bundles")
    project_path: str | None = Field(default=None, description="Optional project path for transcript bundles")
    limit: int = Field(default=20, description="Maximum number of runs to return for list")


class EntityQueryType(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query_type: Literal["by_type", "by_name", "relationships", "traverse"] = Field(description="Type of entity query")
    entity_type: str | None = Field(
        default=None, description="Entity type for 'by_type' (person/place/concept/event/emotion/object)"
    )
    name: str | None = Field(default=None, description="Name for 'by_name' partial match")
    entity_id: str | None = Field(default=None, description="Entity ID for 'relationships' or 'traverse'")
    direction: Literal["in", "out", "both"] = Field(default="both", description="Direction for relationships")
    max_depth: int = Field(default=2, description="Max depth for traversal")


class TemporalWindow(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    window: Literal["today", "week", "month", "year"] = Field(default="week", description="Time window for retrieval")
    trend: str | None = Field(default=None, description="Filter by trend (stable/strengthening/weakening/stale)")
    category: str | None = Field(default=None, description="Category filter")
    limit: int = Field(default=50, description="Max results")


class SystemStatusOptions(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    include_trends: bool = Field(default=False, description="Whether to include temporal trend analysis")
    timeframe: str = Field(default="30 days", description="Timeframe for trend analysis")
    include_cache_metrics: bool = Field(default=False, description="Whether to include cache and budget metrics")
    enforce_hard_caps: bool = Field(
        default=False, description="Whether to enforce hard caps on memory and cache counts"
    )


class EntityAction(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    action: Literal["extract", "link"] = Field(..., description="Action to perform")
    content: str | None = Field(default=None, description="Text content for extraction")
    memory_id: str | None = Field(default=None, description="Memory ID for linking")
    entity_ids: list[str] | None = Field(default=None, description="Entity IDs for linking")


class EntityQuery(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query_type: Literal["by_type", "by_name", "relationships", "traverse"] = Field(..., description="Type of query")
    entity_type: str | None = Field(default=None, description="Entity type filter")
    name: str | None = Field(default=None, description="Name for partial match")
    entity_id: str | None = Field(default=None, description="Entity ID for relationships or starting traversal")
    direction: Literal["in", "out", "both"] = Field(default="both", description="Relationship direction")
    max_depth: int = Field(default=2, description="Traversal depth")
    limit: int = Field(default=50, description="Result limit")


class MemoryAction(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    action: Literal["store", "update", "delete", "archive"] = Field(..., description="Action to perform")
    memory_id: str | None = Field(default=None, description="Memory ID for update/delete/archive")
    content: str | None = Field(default=None, description="Content for store/update")
    options: MemoryOptions | None = Field(default=None, description="Options for store")
    updates: MemoryUpdateOptions | None = Field(default=None, description="Updates for update action")


class VersionAction(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    action: Literal["list", "diff", "rollback"] = Field(..., description="Versioning action")
    memory_id: str = Field(..., description="Memory ID")
    version1: int | None = Field(default=None, description="First version for diff")
    version2: int | None = Field(default=None, description="Second version for diff")
    to_version: int | None = Field(default=None, description="Version to rollback to")


class AnalysisAction(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    action: Literal["synthesize", "reflect"] = Field(..., description="Analysis action")
    period: str = Field(default="weekly", description="Period for reflection")
    limit: int = Field(default=50, description="Limit for synthesis")
    enhanced: bool = Field(default=False, description="Whether to use enhanced synthesis")


class MaintenanceAction(BaseModel):
    modes: list[str] = Field(
        default=["consolidate", "contradict", "archive_stale", "synthesize"],
        description="Maintenance modes to run",
    )
    duplicate_threshold: float = Field(default=0.25, description="Minimum Jaccard similarity to consider duplicate")
    stale_strength_threshold: float = Field(default=0.2, description="Archive memories below this strength")
    stale_importance_threshold: float = Field(default=0.1, description="Archive memories below this importance")
    batch_size: int = Field(default=200, description="Max memories to process per mode")
    max_runtime_seconds: float = Field(default=300, description="Wall-clock budget in seconds")


def _run_async(coro):
    """Run an async coroutine safely, handling existing event loops.

    When an event loop is already running (e.g. inside an MCP server),
    asyncio.run() raises RuntimeError. This helper offloads the coroutine
    to a fresh loop in a background thread instead.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


def _check_rate_limit(tenant_id: str | None = None) -> None:
    """Check rate limit for tenant, raising RateLimitExceededError if exceeded."""
    tid = tenant_id or get_current_account_id()
    # Look up tenant-specific limits from DB
    rate_limit = DEFAULT_RATE_LIMIT
    burst_limit = DEFAULT_BURST_LIMIT
    try:
        conn = get_db_connection()
        row = conn.execute("SELECT rate_limit, burst_limit FROM tenants WHERE id = ?", (tid,)).fetchone()
        conn.close()
        if row:
            rate_limit = row["rate_limit"] or DEFAULT_RATE_LIMIT
            burst_limit = row["burst_limit"] or DEFAULT_BURST_LIMIT
    except Exception:
        pass  # Fall back to defaults if DB unavailable

    limiter = get_rate_limiter()
    if not limiter.acquire(tid, rate_limit=rate_limit, burst_limit=burst_limit):
        remaining = limiter.get_remaining(tid)
        reset_time = time.time() + 60 / rate_limit
        raise RateLimitExceededError(remaining=remaining, reset_time=reset_time)


class PostgresPooledConnection(PooledConnection):
    """Wraps a psycopg connection to match the PooledConnection interface.

    Translates SQLite-flavoured SQL to PostgreSQL dialect on execute().
    Used when FORESIGHT_DB_URL is set and PostgresBackend is active.
    Calling close() returns the connection to the psycopg pool.
    """

    def __init__(self, conn: Any, pool: Any) -> None:
        self._conn = conn
        self._pool: Any = pool
        self._last_result = None
        self._rowcount = -1
        self._released = False

    @staticmethod
    def _adapt_row(row: Any) -> Any:
        if row is None or not isinstance(row, dict):
            return row
        return PostgresRow(row)

    def _ensure_healthy_conn(self):
        """Replace the wrapped connection if Neon or the server closed it."""
        if self._conn.closed or getattr(self._conn, "broken", False):
            if hasattr(self._pool, "putconn"):
                with contextlib.suppress(Exception):
                    self._pool.putconn(self._conn)
            self._conn = self._pool.getconn()

    @staticmethod
    def _is_transient_conn_error(exc: Exception) -> bool:
        """True if *exc* is a connection-level error worth a single retry.

        Covers Neon ``AdminShutdown`` (PG class 57P01), generic ``OperationalError``
        (connection lost mid-query), and ``InterfaceError`` (connection already
        closed/broken).  These are transient: a fresh connection from the pool
        will succeed.
        """
        try:
            import psycopg
            import psycopg.errors
        except ImportError:
            return False
        transient_types = (
            psycopg.OperationalError,
            psycopg.InterfaceError,
            psycopg.errors.AdminShutdown,
        )
        return isinstance(exc, transient_types)

    def _execute_with_retry(self, pg_sql: str, params: tuple) -> Any:
        """Execute *pg_sql* with one automatic retry on transient connection errors.

        Neon serverless Postgres can ``pg_terminate_backend()`` a pooled
        connection between the ``_ensure_healthy_conn()`` check and the actual
        ``execute()`` call (scale-to-zero wake-up race).  This wraps the execute
        in a try/except, refreshes the connection on a transient error, and
        retries exactly once.  A second failure propagates to the caller.
        """
        try:
            return self._conn.execute(pg_sql, params)
        except Exception as exc:
            if not self._is_transient_conn_error(exc):
                raise
            logging.warning("Transient connection error on execute(), refreshing connection: %s", exc)
            self._ensure_healthy_conn()
            return self._conn.execute(pg_sql, params)

    def execute(self, sql, params=()):
        import re

        from foresight.backend.postgres_backend import _translate_sql

        self._ensure_healthy_conn()
        sql_upper = sql.lstrip().upper()
        if sql_upper.startswith("PRAGMA "):
            match = re.search(r"PRAGMA\s+table_info\s*\(\s*([\'\"]?\w+[\'\"]?)\s*\)", sql, re.IGNORECASE)
            if match:
                table_name = match.group(1).strip("'\"")
                pg_sql = "SELECT 0 as cid, column_name as name, 'TEXT' as type, 0 as notnull, NULL as dflt_value, 0 as pk FROM information_schema.columns WHERE table_name = %s"
                self._last_result = self._execute_with_retry(pg_sql, (table_name,))
                self._rowcount = self._last_result.rowcount
                return self

            class DummyResult:
                rowcount = 0

                def fetchone(self):
                    return None

                def fetchall(self):
                    return []

            self._last_result = DummyResult()
            self._rowcount = 0
            return self

        pg_sql = _translate_sql(sql)
        self._last_result = self._execute_with_retry(pg_sql, params)
        self._rowcount = self._last_result.rowcount
        return self

    def execute_returning(self, sql, params=()):
        from foresight.backend.postgres_backend import _translate_sql

        pg_sql = _translate_sql(sql)
        upper_sql = pg_sql.upper()
        if " RETURNING " not in upper_sql and upper_sql.lstrip().startswith(("INSERT ", "UPDATE ", "DELETE ")):
            pg_sql = f"{pg_sql} RETURNING *"
        self._last_result = self._execute_with_retry(pg_sql, params)
        self._rowcount = self._last_result.rowcount
        return self

    @property
    def rowcount(self):
        return self._rowcount

    def fetchone(self):
        if self._last_result is None:
            return None
        return self._adapt_row(self._last_result.fetchone())

    def fetchall(self):
        if self._last_result is None:
            return []
        return [self._adapt_row(row) for row in self._last_result.fetchall()]

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if not self._released:
            self._released = True
            # Commit any pending transaction before returning to the pool.
            # Without this, the pool rolls back open transactions (INTRANS),
            # discarding writes from code paths that don't call commit() explicitly.
            with contextlib.suppress(Exception):
                self._conn.commit()
            if hasattr(self._pool, "release"):
                self._pool.release(self)
            elif hasattr(self._pool, "putconn"):
                self._pool.putconn(self._conn)

    @property
    def row_factory(self):
        return dict

    @row_factory.setter
    def row_factory(self, value):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class PostgresRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _supports_execute_returning(conn: Any) -> bool:
    return hasattr(conn, "execute_returning") and callable(conn.execute_returning)


def get_db_connection(db_path: str | None = None):
    """Get a Postgres connection from the shared backend pool.

    Foresight is Postgres-only. The backend must be initialized first via
    ``_initialize_backend()`` (which requires ``FORESIGHT_DB_URL``). There is
    deliberately no SQLite fallback: if the backend is not a Postgres backend,
    this raises ``RuntimeError`` instead of silently opening a local SQLite
    store. That dual-store behavior is exactly the bug this migration removes.

    Returns
    -------
    PostgresPooledConnection
        A connection-like object with ``execute()``, ``fetchone()``,
        ``fetchall()``, ``commit()``, and ``close()``.
    """
    if _global_backend is None:
        raise RuntimeError(
            "Database backend not initialized. Call _initialize_backend() first "
            "(requires FORESIGHT_DB_URL). Foresight is Postgres-only; no SQLite fallback."
        )
    if _global_backend.backend_type != "postgresql":
        raise RuntimeError(
            f"Database backend is '{_global_backend.backend_type}', expected 'postgresql'. "
            "Foresight is Postgres-only; no SQLite fallback."
        )
    _log = logging.getLogger("foresight.db")
    _log.debug("get_db_connection: using PostgresBackend pool")
    pool = _global_backend._pool
    conn = pool.getconn()
    return PostgresPooledConnection(conn, pool)


SCHEMA_VERSION = 14


def _seed_default_tenant(conn) -> None:
    """Insert the default tenant row if it does not already exist."""
    conn.execute(
        """
        INSERT INTO tenants (id, name, rate_limit, burst_limit, created_at, config)
        VALUES (?, 'Default tenant', ?, ?, ?, '{}')
        ON CONFLICT (id) DO NOTHING
        """,
        (DEFAULT_TENANT_ID, DEFAULT_RATE_LIMIT, DEFAULT_BURST_LIMIT, datetime.now(timezone.utc).isoformat()),
    )


_SCHEMA_MIGRATIONS = {
    1: [
        """CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            rate_limit INTEGER DEFAULT 100,
            burst_limit INTEGER DEFAULT 20,
            created_at TEXT NOT NULL,
            config TEXT DEFAULT '{}'
        )""",
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            scope TEXT DEFAULT 'session',
            retention TEXT DEFAULT 'short_term',
            category TEXT DEFAULT 'fact',
            user_id TEXT DEFAULT 'default',
            bank_id TEXT DEFAULT 'default',
            created_at TEXT NOT NULL,
            updated_at TEXT,
            tags TEXT DEFAULT '[]',
            emotional_context TEXT DEFAULT '{}',
            metrics TEXT DEFAULT '{}',
            vector_id TEXT,
            gist TEXT,
            is_ghost INTEGER DEFAULT 0,
            synthesized_from TEXT DEFAULT '[]',
            version INTEGER DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS memory_versions (
            id TEXT PRIMARY KEY,
            memory_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            content TEXT NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            tags TEXT DEFAULT '[]',
            emotional_context TEXT DEFAULT '{}',
            metrics TEXT DEFAULT '{}',
            rollback_of TEXT DEFAULT NULL,
            FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content)",
        "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)",
        "CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories(tags)",
        "CREATE INDEX IF NOT EXISTS idx_versions_memory ON memory_versions(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_versions_tenant ON memory_versions(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_versions_created ON memory_versions(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_tenants_id ON tenants(id)",
    ],
    2: [
        "ALTER TABLE memories ADD COLUMN accessed_at TEXT DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE memories ADD COLUMN importance REAL DEFAULT 1.0",
        "ALTER TABLE memories ADD COLUMN decay_rate REAL DEFAULT 0.01",
        "ALTER TABLE memories ADD COLUMN activation_count INTEGER DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN retrieval_count INTEGER DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN strength_trend TEXT DEFAULT 'stable'",
        "ALTER TABLE memories ADD COLUMN last_retrieved_at TEXT",
        "ALTER TABLE memories ADD COLUMN category TEXT DEFAULT 'general'",
        # PIX-3956 clinical/safety/privacy gating. ALTER is idempotent on
        # re-runs because the runner catches "duplicate column" errors and
        # skips them. sensitivity_reason is nullable so legacy rows can
        # stay at NULL.
        "ALTER TABLE memories ADD COLUMN is_sensitive INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN sensitivity_reason TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_user_created ON memories(user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memories_user_accessed ON memories(user_id, accessed_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(user_id, importance DESC, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_strength_trend ON memories(user_id, strength_trend, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(user_id, category, created_at DESC)",
        """CREATE TABLE IF NOT EXISTS decay_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            half_life_hours REAL DEFAULT 168.0,
            min_importance REAL DEFAULT 0.1,
            activation_boost REAL DEFAULT 1.2,
            strengthening_threshold INTEGER DEFAULT 5,
            stale_threshold REAL DEFAULT 0.2,
            UNIQUE(tenant_id, user_id, category)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_decay_config_tenant ON decay_config(tenant_id)",
    ],
    3: [
        """CREATE TABLE IF NOT EXISTS curation_runs (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            source_bank_id TEXT NOT NULL,
            output_bank_id TEXT NOT NULL,
            policy_mode TEXT NOT NULL,
            tool_access TEXT NOT NULL,
            output_mode TEXT NOT NULL,
            status TEXT NOT NULL,
            instructions TEXT,
            summary_json TEXT DEFAULT '{}',
            error_json TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            archived_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_curation_runs_tenant_user ON curation_runs(tenant_id, user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_curation_runs_status ON curation_runs(tenant_id, user_id, status, created_at DESC)",
    ],
    4: [
        """CREATE TABLE IF NOT EXISTS context_blocks (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            label TEXT NOT NULL,
            content TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, label)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_context_blocks_lookup ON context_blocks(tenant_id, user_id, updated_at DESC)",
    ],
    5: [
        "ALTER TABLE curation_runs ADD COLUMN transcript_bundle_json TEXT",
        "ALTER TABLE curation_runs ADD COLUMN session_id TEXT",
        "ALTER TABLE curation_runs ADD COLUMN project_path TEXT",
    ],
    6: [
        """CREATE TABLE IF NOT EXISTS memory_relationships (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            source_memory_id TEXT NOT NULL,
            target_memory_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL
                CHECK(relationship_type IN (
                    'updates', 'extends', 'derives',
                    'contradicts', 'supports', 'related'
                )),
            confidence REAL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
            metadata TEXT DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(tenant_id, user_id, source_memory_id, target_memory_id, relationship_type),
            FOREIGN KEY (source_memory_id) REFERENCES memories(id) ON DELETE CASCADE,
            FOREIGN KEY (target_memory_id) REFERENCES memories(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_relationships_source ON memory_relationships(tenant_id, user_id, source_memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_relationships_target ON memory_relationships(tenant_id, user_id, target_memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_relationships_type ON memory_relationships(tenant_id, user_id, relationship_type)",
        "ALTER TABLE memories ADD COLUMN relation_type TEXT",
        "ALTER TABLE memories ADD COLUMN related_memory_id TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_relation ON memories(tenant_id, user_id, relation_type)",
    ],
    7: [
        """CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            vector BLOB NOT NULL,
            model_version TEXT DEFAULT '1',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id, memory_id, provider)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_embeddings_user ON memory_embeddings(tenant_id, user_id, provider)",
    ],
    8: [
        """CREATE TABLE IF NOT EXISTS documents (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            chunk_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            UNIQUE(tenant_id, user_id, content_hash)
        )""",
        """CREATE TABLE IF NOT EXISTS document_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            memory_id TEXT,
            chunk_index INTEGER NOT NULL,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(document_id, chunk_index),
            FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_documents_user ON documents(tenant_id, user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(tenant_id, user_id, content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_doc ON document_chunks(document_id, chunk_index)",
        "CREATE INDEX IF NOT EXISTS idx_document_chunks_memory ON document_chunks(memory_id)",
    ],
    9: [
        "ALTER TABLE memories ADD COLUMN current_strength REAL",
        "ALTER TABLE memories ADD COLUMN last_decay_at TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_strength ON memories(tenant_id, user_id, current_strength)",
        """CREATE TABLE IF NOT EXISTS memory_decay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            memory_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            old_strength REAL,
            new_strength REAL,
            decay_factor REAL,
            reason TEXT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_memory_decay_events_lookup ON memory_decay_events (tenant_id, user_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_memory_decay_events_memory ON memory_decay_events (memory_id, created_at DESC)",
    ],
    10: [
        "ALTER TABLE memories ADD COLUMN content_hash TEXT",
        "CREATE INDEX IF NOT EXISTS idx_memories_content_hash ON memories(tenant_id, user_id, content_hash)",
    ],
    11: [
        "CREATE INDEX IF NOT EXISTS idx_memories_tenant_user_created ON memories(tenant_id, user_id, created_at DESC)",
    ],
    # v12 — Register 11 tables that individual modules create inline with
    # CREATE TABLE IF NOT EXISTS, plus 002_unified_schema columns. Mirrors
    # foresight/backend/schema_ddl.py MIGRATIONS[12].
    12: [
        # 002_unified_schema columns
        "ALTER TABLE memories ADD COLUMN schema_version TEXT",
        "ALTER TABLE memories ADD COLUMN source_service TEXT",
        # graph_store tables
        """CREATE TABLE IF NOT EXISTS memory_entities (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL
                CHECK(entity_type IN ('person', 'place', 'concept', 'event', 'emotion', 'object', 'cluster')),
            description TEXT,
            properties TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tenant_id, user_id, name, entity_type)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_entities_user ON memory_entities(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_entities_tenant ON memory_entities(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_memory_entities_tenant ON memory_entities(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_entities_type ON memory_entities(entity_type)",
        "CREATE INDEX IF NOT EXISTS idx_entities_name ON memory_entities(name)",
        """CREATE TABLE IF NOT EXISTS entity_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            source_entity_id TEXT NOT NULL,
            target_entity_id TEXT NOT NULL,
            relationship_type TEXT NOT NULL
                CHECK(relationship_type IN (
                    'mentions', 'located_at', 'experienced', 'caused',
                    'relates_to', 'contradicts', 'supports', 'part_of', 'created'
                )),
            confidence REAL DEFAULT 1.0 CHECK(confidence >= 0 AND confidence <= 1),
            last_accessed TEXT DEFAULT CURRENT_TIMESTAMP,
            decay_factor REAL DEFAULT 1.0 CHECK(decay_factor >= 0 AND decay_factor <= 1),
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(tenant_id, user_id, source_entity_id, target_entity_id, relationship_type)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_rel_source ON entity_relationships(source_entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_rel_target ON entity_relationships(target_entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_rel_user ON entity_relationships(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_rel_tenant ON entity_relationships(tenant_id)",
        """CREATE TABLE IF NOT EXISTS memory_entity_links (
            memory_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            relevance_score REAL DEFAULT 1.0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (memory_id, entity_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_links_memory ON memory_entity_links(memory_id)",
        "CREATE INDEX IF NOT EXISTS idx_links_entity ON memory_entity_links(entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_links_user ON memory_entity_links(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_links_tenant ON memory_entity_links(tenant_id)",
        # auth tables
        """CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            role TEXT NOT NULL,
            is_active BOOLEAN DEFAULT true,
            created_at TEXT NOT NULL,
            last_login TEXT,
            password_hash TEXT NOT NULL,
            api_key TEXT UNIQUE NOT NULL,
            tenant_access TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
        "CREATE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key)",
        """CREATE TABLE IF NOT EXISTS auth_sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            ip_address TEXT,
            user_agent TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON auth_sessions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON auth_sessions(expires_at)",
        # event_bus tables
        """CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            actor TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            metadata TEXT DEFAULT '{}'
        )""",
        "CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)",
        "CREATE INDEX IF NOT EXISTS idx_events_tenant ON events(tenant_id)",
        # audit tables
        """CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at REAL NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_time ON audit_events(tenant_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_audit_events_type ON audit_events(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_audit_events_resource ON audit_events(resource_id)",
        "CREATE INDEX IF NOT EXISTS idx_audit_events_tenant_type ON audit_events(tenant_id, event_type)",
        # hooks tables
        """CREATE TABLE IF NOT EXISTS hooks (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            hook_type TEXT NOT NULL,
            handler TEXT NOT NULL,
            condition_name TEXT,
            retry_count INTEGER DEFAULT 3,
            timeout INTEGER DEFAULT 30,
            metadata TEXT DEFAULT '{}',
            enabled INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_hooks_event_type ON hooks(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_hooks_enabled ON hooks(enabled)",
        "CREATE INDEX IF NOT EXISTS idx_hooks_tenant ON hooks(tenant_id)",
        # sync tables
        """CREATE TABLE IF NOT EXISTS operations (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            type TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            retry_count INTEGER DEFAULT 0,
            last_attempt TEXT,
            vector_clock TEXT DEFAULT '{}'
        )""",
        "CREATE INDEX IF NOT EXISTS idx_ops_entity ON operations(entity_type, entity_id)",
        "CREATE INDEX IF NOT EXISTS idx_ops_created ON operations(created_at)",
        "CREATE INDEX IF NOT EXISTS idx_ops_tenant ON operations(tenant_id)",
        # narrative_cache table
        """CREATE TABLE IF NOT EXISTS narrative_cache (
            cache_key TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            report_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            insights_hash TEXT NOT NULL,
            narrative TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            last_accessed_at DOUBLE PRECISION NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_narrative_cache_tenant_user ON narrative_cache(tenant_id, user_id)",
    ],
    13: [
        "ALTER TABLE narrative_cache ALTER COLUMN created_at TYPE DOUBLE PRECISION",
        "ALTER TABLE narrative_cache ALTER COLUMN last_accessed_at TYPE DOUBLE PRECISION",
    ],
    14: [
        """CREATE TABLE IF NOT EXISTS memory_merge_history (
            id TEXT PRIMARY KEY,
            primary_id TEXT NOT NULL,
            merged_ids TEXT NOT NULL DEFAULT '[]',
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT,
            cluster_id TEXT,
            avg_overlap REAL,
            merged_at TEXT NOT NULL,
            merged_by TEXT DEFAULT 'system',
            pre_merge_content TEXT,
            merged_content TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_merge_history_primary ON memory_merge_history(primary_id)",
        "CREATE INDEX IF NOT EXISTS idx_merge_history_tenant ON memory_merge_history(tenant_id)",
        "CREATE INDEX IF NOT EXISTS idx_merge_history_user ON memory_merge_history(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_merge_history_merged_at ON memory_merge_history(merged_at)",
    ],
}


def init_db(backend=None):
    """Initialize the database schema with idempotent versioned migrations.

    Args:
        backend: Optional DatabaseBackend. None → create a Postgres backend
                 via ``create_backend()`` (requires ``FORESIGHT_DB_URL``).
    """
    if backend is None:
        db_path = Path(DB_PATH) if DB_PATH else None
        if db_path:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        backend = create_backend()
    backend.connect()

    try:
        # Reconcile any wrong-shaped schema_migrations table (e.g. the app's
        # id/name/executed_at shape) before reading applied versions, so a
        # shared database can never crash init_db with UndefinedColumn.
        ensure_schema_migrations_table(backend)

        applied = {row["version"] for row in backend.fetch("SELECT version FROM schema_migrations")}

        for version in sorted(_SCHEMA_MIGRATIONS):
            if version in applied:
                continue
            for stmt in _SCHEMA_MIGRATIONS[version]:
                try:
                    backend.execute(stmt)
                except Exception as e:
                    err = str(e).lower()
                    if "duplicate column" in err or "already exists" in err:
                        continue
                    if "sqlite" in backend.__class__.__name__.lower() and (
                        "alter column" in stmt.lower() or 'near "type"' in err
                    ):
                        continue
                    raise
            backend.set_version(version, datetime.now(timezone.utc).isoformat())

        # Ensure the built-in default tenant always exists so tenant switches are stable.
        # Use ON CONFLICT DO NOTHING (works on both SQLite 3.24+ and PostgreSQL)
        # instead of _seed_default_tenant(conn) which uses SQLite-specific INSERT OR IGNORE.
        backend.execute(
            """
            INSERT INTO tenants (id, name, rate_limit, burst_limit, created_at, config)
            VALUES (?, 'Default tenant', ?, ?, ?, '{}')
            ON CONFLICT (id) DO NOTHING
            """,
            (
                DEFAULT_TENANT_ID,
                DEFAULT_RATE_LIMIT,
                DEFAULT_BURST_LIMIT,
                datetime.now(timezone.utc).isoformat(),
            ),
        )

        # Migrate decay_config: add tenant_id if table exists without it
        if backend.table_exists("decay_config") and not backend.column_exists("decay_config", "tenant_id"):
            backend.execute("ALTER TABLE decay_config ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
            backend.execute("CREATE INDEX IF NOT EXISTS idx_decay_config_tenant ON decay_config(tenant_id)")

        # Backfill content_hash for existing memories (v10 migration)
        try:
            rows = backend.fetch("SELECT id, content FROM memories WHERE content_hash IS NULL")
            if rows:
                for row in rows:
                    h = _content_hash(row["content"])
                    backend.execute("UPDATE memories SET content_hash = ? WHERE id = ?", (h, row["id"]))
        except Exception:
            pass  # Table doesn't exist yet; will be created by migrations
    finally:
        backend.close()


def _initialize_backend() -> None:
    """Create and connect the global database backend.

    ``FORESIGHT_DB_URL`` is **required**.  There is no SQLite fallback.
    If the URL is set but the connection fails, the server aborts with a
    clear error message.  If the URL is unset, a ``RuntimeError`` is raised.

    Idempotent: if ``_global_backend`` is already set, this is a no-op.
    """
    global _global_backend
    if _global_backend is not None:
        return
    if not os.environ.get("FORESIGHT_DB_URL"):
        raise RuntimeError(
            "FORESIGHT_DB_URL is not set.  Foresight requires a Postgres DSN. "
            "SQLite-as-primary is no longer supported.\n"
            "Example: export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'"
        )
    try:
        backend = create_backend()
        backend.connect()
        _global_backend = backend
        logger.info("Database backend initialised (type=%s)", type(backend).__name__)
    except Exception:
        logger.exception(
            "FATAL: FORESIGHT_DB_URL is set (%s) but the backend failed to connect. "
            "Aborting startup. Fix the connection string.",
            os.environ.get("FORESIGHT_DB_URL"),
        )
        raise


def _initialize_redis() -> None:
    """Create and connect the global RedisCompanion (PIX-3995).

    If ``REDIS_URL`` is set, creates a ``RedisCompanion`` and eagerly
    connects.  The companion is stored as ``_redis_companion`` and
    gracefully degrades if Redis is unreachable.
    """
    global _redis_companion  # noqa: PLW0603
    if not REDIS_URL:
        logger.debug("No REDIS_URL set; RedisCompanion disabled")
        return
    companion = RedisCompanion(REDIS_URL)
    _run_async(companion._ensure_connected())  # eager connect
    _redis_companion = companion


# Initialize database on module load - deferred to runtime in main()
# init_db()  # Deferred initialization

# Initialize memory system components
_SERVER_STATE: dict[str, Any] = {
    "memory_system_initialized": False,
    "safe_path_prefixes": None,
    "sync_manager": None,
    "tenant_context": None,
}

_CURATION_WORKERS: dict[str, threading.Thread] = {}
_CURATION_WORKERS_LOCK = threading.Lock()
_CURATION_CANCEL_SIGNALS: dict[str, threading.Event] = {}


class CurationError(RuntimeError):
    """Raised when a curation run is canceled before publication completes."""


# =============================================================================
# Version Management Functions
# =============================================================================


def get_memory_versions(memory_id: str, user_id: str | None = None) -> str:
    """Get all versions of a memory."""
    uid = user_id or USER_ID
    conn = get_db_connection()

    # Verify memory exists
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, get_current_account_id()),
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."

    # Get current version
    current_version = row["version"] if row else 1

    # Get version history
    versions = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND tenant_id = ? ORDER BY version DESC",
        (memory_id, get_current_account_id()),
    ).fetchall()
    conn.close()

    if not versions:
        return f"Memory {memory_id} (version {current_version}): No version history found."

    result = [f"Memory {memory_id} - {len(versions)} versions:", ""]
    for v in versions:
        result.append(f"  v{v['version']}: {v['content'][:50]}...")
        result.append(f"    Created: {v['created_at']}")
        if v["rollback_of"]:
            result.append(f"    Rollback of: {v['rollback_of']}")

    return "\n".join(result)


def create_version_snapshot(memory_id: str, data: dict) -> str:
    """Create a new version snapshot for a memory."""
    version = data.get("version", 1)
    version_id = str(hashlib.sha256(f"{memory_id}:{version}".encode()).hexdigest())[:16]

    # Handle stringified inputs from DB rows
    tags = data.get("tags", "[]")
    emo = data.get("emotional_context")
    met = data.get("metrics")

    tags_json = tags if isinstance(tags, str) else json.dumps(tags)
    emo_json = emo if isinstance(emo, str) else json.dumps(emo)
    met_json = met if isinstance(met, str) else json.dumps(met)

    tenant_id = data.get("tenant_id") or get_current_account_id()

    conn = get_db_connection()
    conn.execute(
        """
    INSERT INTO memory_versions (
        id, memory_id, tenant_id, content, version, created_at, tags, emotional_context, metrics, rollback_of
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            version_id,
            memory_id,
            tenant_id,
            data["content"],
            version,
            datetime.now(timezone.utc).isoformat(),
            tags_json,
            emo_json,
            met_json,
            data.get("rollback_of"),
        ),
    )
    conn.commit()
    conn.close()
    return version_id


def rollback_to_version(memory_id: str, target_version: int, user_id: str | None = None) -> str:
    """Rollback a memory to a specific version."""
    uid = user_id or USER_ID
    tenant_id = get_current_account_id()
    conn = get_db_connection()

    # Verify memory ownership first
    current = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (memory_id, uid, tenant_id),
    ).fetchone()

    if not current:
        conn.close()
        return f"Memory {memory_id} not found"

    # Get the version content (tenant enforced via memory ownership above)
    version_row = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (memory_id, target_version, tenant_id),
    ).fetchone()

    if not version_row:
        conn.close()
        return f"Version {target_version} not found for memory {memory_id}"

    # Snapshot current state before rollback
    create_version_snapshot(
        memory_id=memory_id,
        data={
            "content": current["content"],
            "tags": current["tags"],
            "emotional_context": current["emotional_context"],
            "metrics": current["metrics"],
            "version": current["version"] or 1,
            "rollback_of": None,
            "tenant_id": current["tenant_id"],
        },
    )

    # Update to target version content
    new_version = target_version + 1
    conn.execute(
        """
    UPDATE memories SET
        content = ?, tags = ?, emotional_context = ?, metrics = ?,
        version = ?, updated_at = ?
    WHERE id = ? AND user_id = ? AND tenant_id = ?
    """,
        (
            version_row["content"],
            version_row["tags"],
            version_row["emotional_context"],
            version_row["metrics"],
            new_version,
            datetime.now(timezone.utc).isoformat(),
            memory_id,
            uid,
            tenant_id,
        ),
    )
    conn.commit()
    conn.close()

    # Emit rollback event
    event_bus = get_event_bus_with_stream()
    # Redact sensitive content from event bus publishes
    content_redacted = "[REDACTED - sensitive]" if current["is_sensitive"] else current["content"]
    version_is_sensitive = "is_sensitive" in version_row and bool(version_row["is_sensitive"])
    new_content_redacted = "[REDACTED - sensitive]" if version_is_sensitive else version_row["content"]
    event_bus.publish(
        memory_updated(memory_id=memory_id, old_content=content_redacted, new_content=new_content_redacted, actor=uid)
    )

    return f"Rolled back memory {memory_id} to version {target_version}"


def get_memory_diff(memory_id: str, version1: int, version2: int, _user_id: str | None = None) -> dict[str, Any]:
    """Get diff between two versions of a memory."""
    conn = get_db_connection()
    tenant_id = get_current_account_id()

    v1 = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (memory_id, version1, tenant_id),
    ).fetchone()

    v2 = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (memory_id, version2, tenant_id),
    ).fetchone()

    conn.close()

    if not v1 or not v2:
        return {"error": "One or both versions not found"}

    return {
        "memory_id": memory_id,
        "version1": {"version": version1, "content": v1["content"]},
        "version2": {"version": version2, "content": v2["content"]},
        "changed_fields": ["content"],
    }


# =============================================================================
# Memory System Components
# =============================================================================


def get_memory_system():
    """Get or initialize the memory system components."""
    if not _SERVER_STATE["memory_system_initialized"]:
        _SERVER_STATE["memory_system_initialized"] = True
    return {
        "tagger": MemoryCrisisTagger(get_crisis_service("high")),
        "gate": None,
        "synthesizer": MemorySynthesizer(),
        "linker": MemoryLinker(),
    }


class RateLimitMiddleware(_Middleware):
    """FastMCP middleware that enforces per-tenant rate limiting on tool calls."""

    async def on_call_tool(self, context, call_next):
        try:
            _check_rate_limit()
        except RateLimitExceededError as e:
            # Sleep to prevent tight agent retry loops that burn API tokens
            await asyncio.sleep(5.0)
            return ToolResult(
                content=[TextContent(type="text", text=str(e))],
                meta={"isError": True},
            )
        return await call_next(context)


_MAX_CONTENT_LENGTH = 100_000
_MAX_QUERY_LENGTH = 10_000
_MAX_LIMIT = 1000
_MAX_TENANT_ID_LENGTH = 64
_MAX_USER_ID_LENGTH = 128


def _validate_lengths(arguments: dict) -> str | None:
    for key in ("content", "conversation_text", "transcript"):
        val = arguments.get(key)
        if isinstance(val, str) and len(val) > _MAX_CONTENT_LENGTH:
            return f"{key} exceeds maximum length of {_MAX_CONTENT_LENGTH} characters"

    for key in ("query",):
        val = arguments.get(key)
        if isinstance(val, str) and len(val) > _MAX_QUERY_LENGTH:
            return f"{key} exceeds maximum length of {_MAX_QUERY_LENGTH} characters"
    return None


def _validate_numeric(arguments: dict) -> str | None:
    for key in ("limit",):
        val = arguments.get(key)
        if val is not None:
            try:
                limit_val = int(val)
                if limit_val > _MAX_LIMIT:
                    return f"limit cannot exceed {_MAX_LIMIT}"
                if limit_val < 0:
                    return "limit cannot be negative"
            except (ValueError, TypeError):
                return f"{key} must be an integer"
    for key in ("offset",):
        val = arguments.get(key)
        if val is not None:
            try:
                if int(val) < 0:
                    return "offset cannot be negative"
            except (ValueError, TypeError):
                return f"{key} must be a non-negative integer"
    return None


def _validate_ids(arguments: dict) -> str | None:
    for key in ("user_id", "tenant_id"):
        val = arguments.get(key)
        if val is not None and isinstance(val, str):
            if key == "tenant_id" and len(val) > _MAX_TENANT_ID_LENGTH:
                return f"{key} exceeds maximum length of {_MAX_TENANT_ID_LENGTH}"
            if key == "user_id" and len(val) > _MAX_USER_ID_LENGTH:
                return f"{key} exceeds maximum length of {_MAX_USER_ID_LENGTH}"
    return None


def _validate_paths(arguments: dict) -> str | None:
    if _SERVER_STATE["safe_path_prefixes"] is None:
        _SERVER_STATE["safe_path_prefixes"] = [
            str(Path.home()),
            os.getcwd(),
            "/tmp",
        ]

    for key in ("output_path", "path", "file_path"):
        val = arguments.get(key)
        if val is not None and isinstance(val, str):
            if ".." in val:
                return "Path traversal not allowed"
            if not any(val.startswith(p) for p in _SERVER_STATE["safe_path_prefixes"]):
                return f"Access to {val} is restricted"
    return None


def _validate_tool_inputs(_name: str, arguments: dict) -> str | None:
    """Validate tool inputs."""
    for validator in (_validate_lengths, _validate_numeric, _validate_ids, _validate_paths):
        error = validator(arguments)
        if error:
            return error
    return None


class InputValidationMiddleware(_Middleware):
    """FastMCP middleware that validates tool inputs."""

    async def on_call_tool(self, context, call_next):
        try:
            name = getattr(context, "name", None) or getattr(context, "tool_name", None) or ""
            arguments = getattr(context, "arguments", {}) or {}
            error = _validate_tool_inputs(name, arguments)
            if error:
                return ToolResult(
                    content=[TextContent(type="text", text=f"Validation error: {error}")],
                    meta={"isError": True},
                )
        except Exception:
            logger.debug("Input validation check failed; proceeding to tool call")
        return await call_next(context)


FORESIGHT_SERVER_INSTRUCTIONS = """Foresight provides persistent memory, continuity context blocks, and background curation for AI agents.

### Guidelines for AI Agents:
1. Context Continuity: At session start or when switching topics, call `inject_context` or `manage_context_blocks` to load active guidance, user preferences, and relevant memory items into context.
2. Memory Scopes & Retention:
   - `session`: Ephemeral working memory for the immediate task/session.
   - `arc`: Medium-term project milestone or ongoing objective.
   - `fact`: Durable facts and verified project knowledge.
   - `trait`: Enduring user habits, communication preferences, and core principles.
3. Proactive Capture: Store key user preferences, architectural decisions, and explicit user feedback using `manage_memories(action="store", ...)`.
4. Curation: Use `manage_curation_runs` to inspect and review memory consolidation or archiving proposals without loss of durable context."""

mcp = FastMCP(
    "Foresight",
    instructions=FORESIGHT_SERVER_INSTRUCTIONS,
    website_url="https://foresight.vectorize.io",
    version="0.19.0",
    middleware=[AuthMiddleware(), TenantMiddleware(), InputValidationMiddleware(), RateLimitMiddleware()],
)

logger = logging.getLogger("foresight_server")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)

RowLike = Mapping[str, Any]


def _health_status_dict() -> dict[str, Any]:
    """Best-effort snapshot of the Foresight server's liveness.

    Returns structured dict for /health. Mirrors `foresight doctor`-style fields:
    status (ok/degraded), uptime, plus a trimmed system_status payload.
    """
    started_at = _SERVER_STATE.get("started_at")
    uptime_seconds: float | None = None
    if isinstance(started_at, (int, float)):
        uptime_seconds = max(0.0, time.time() - float(started_at))

    snapshot: dict[str, Any] = {
        "status": "ok",
        "name": getattr(mcp, "name", "Foresight"),
        "uptime_seconds": uptime_seconds,
        "server": "foresight",
    }
    try:
        # Deferred: get_system_status is defined later in this module (~L3960).
        # Bare name fails if called during module init before that def.
        from foresight.server import get_system_status as _get_status

        raw = _get_status()
        parsed: Any = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(parsed, dict):
            status_value = parsed.get("status", "ok")
            if hasattr(status_value, "value"):
                status_value = status_value.value
            if str(status_value).lower() == "degraded":
                snapshot["status"] = "degraded"
            snapshot["memory_count"] = parsed.get("memory_count")
            snapshot["tenant_id"] = parsed.get("tenant_id")
    except Exception as exc:
        import traceback

        logger.error(
            "_health_status_dict: get_system_status raised %s: %s\n%s", type(exc).__name__, exc, traceback.format_exc()
        )
        snapshot["status"] = "degraded"
        snapshot["system_status_error"] = f"{type(exc).__name__}: {exc}"
    return snapshot


@mcp.custom_route("/health", methods=["GET"])
async def health_endpoint(_request: Request) -> JSONResponse:
    """HTTP health probe for load balancers and orchestrators.

    Returns 200 with a JSON body as long as the FastMCP server is alive. We
    tolerate database hiccups here -- the goal is mount-point liveness, not
    data-plane correctness. Tools themselves enforce their own validation.
    """
    payload = _health_status_dict()
    code = 200 if payload.get("status") in {"ok", "degraded"} else 503
    return JSONResponse(payload, status_code=code)


def _cleanup_background_systems() -> None:
    """Gracefully stop background threads and release resources on exit."""
    # Suppress logging tracebacks during interpreter shutdown (streams may be closed)
    logging.raiseExceptions = False

    # Mark sync manager offline
    sync_mgr = _SERVER_STATE.get("sync_manager")
    if sync_mgr is not None:
        try:
            sync_mgr.set_online(False)
            logger.info("Sync manager set offline")
        except Exception as e:
            logger.error(f"Error stopping sync manager: {e}")
        finally:
            _SERVER_STATE["sync_manager"] = None

    # Close database backend
    global _global_backend
    if _global_backend is not None:
        try:
            _global_backend.close()
            logger.info("Database backend closed")
        except Exception as e:
            logger.error(f"Error closing database backend: {e}")
        finally:
            _global_backend = None

    # Release all pooled connections
    try:
        reset_pool()
        logger.info("Connection pool reset")
    except Exception as e:
        logger.error(f"Error resetting connection pool: {e}")


atexit.register(_cleanup_background_systems)


def get_event_bus_with_stream():
    return get_event_bus()


def _handle_memory_store(uid: str, tenant_id: str, options: MemoryAction) -> str:
    """Helper to handle memory storage."""
    if not options.content:
        return "Error: Content is required for 'store' action"
    content = options.content.strip()
    if not content:
        return "Error: Content is required for 'store' action"
    opts = options.options or MemoryOptions()

    # ── PRE_STORE hook ─────────────────────────────────────────────────
    hook_ctx = MemoryHookContext(
        action="store",
        user_id=uid,
        tenant_id=tenant_id,
        content=content,
        category=opts.category,
        scope=getattr(opts, "scope", None),
        retention=getattr(opts, "retention", None),
        importance=getattr(opts, "importance", 0.5),
        tags=getattr(opts, "tags", None),
    )
    for r in get_memory_hook_registry().emit_pre(MemoryHookType.PRE_STORE, hook_ctx):
        if r.abort:
            return f"Hook aborted store: {r.message}"
        if r.modified_context:
            if "content" in r.modified_context:
                options.content = r.modified_context["content"]
            if "category" in r.modified_context:
                opts.category = r.modified_context["category"]

    # Hard cap enforcement: check memory count per tenant
    conn = get_db_connection()
    current_count = (
        conn.execute(
            "SELECT COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0",
            (uid, tenant_id),
        ).fetchone()
        or (0,)
    )[0]
    if current_count >= DEFAULT_MAX_MEMORY_PER_TENANT:
        conn.close()
        return f"Error: Memory limit reached ({DEFAULT_MAX_MEMORY_PER_TENANT} per tenant). Cannot store new memory."
    memory_id = hashlib.sha256(f"{content}{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:16]
    content_h = _content_hash(content)
    existing = conn.execute(
        "SELECT id, activation_count FROM memories "
        "WHERE user_id = ? AND tenant_id = ? AND content_hash = ? AND is_ghost = 0 "
        "ORDER BY created_at DESC LIMIT 1",
        (uid, tenant_id, content_h),
    ).fetchone()
    if existing:
        update_sql = (
            "UPDATE memories SET activation_count = activation_count + 1, updated_at = ? "
            "WHERE id = ? AND user_id = ? AND tenant_id = ?"
        )
        update_params = (datetime.now(timezone.utc).isoformat(), existing["id"], uid, tenant_id)
        if _supports_execute_returning(conn):
            conn.execute_returning(update_sql, update_params).fetchone()
        else:
            conn.execute(update_sql, update_params)
        conn.commit()
        conn.close()
        return f"Duplicate detected - bumped activation for existing memory {existing['id']}"
    # Parse emotional context and metrics
    emo_ctx = EmotionalMetadata.from_dict(opts.emotional_context) if opts.emotional_context else None
    met = EmpathyMetrics.from_dict(opts.metrics) if opts.metrics else None
    memory = MemoryObject.create(
        content=content,
        scope=cast(MemoryScope, opts.scope),
        retention=cast(RetentionPolicy, opts.retention),
        emotional_context=emo_ctx,
        metrics=met,
    )
    memory.id = memory_id
    # Socratic Gate
    ms = get_memory_system()
    gate_result = _run_async(SocraticGate(ms["tagger"]).evaluate(memory, uid))
    memory.tags = gate_result.suggested_tags
    if opts.category and opts.category not in memory.tags:
        memory.tags.append(opts.category)
    # Re-evaluate sensitivity at INSERT-time — content may have been
    # mutated by a PRE_STORE hook above.
    is_sensitive_bit, sensitivity_reason = resolve_is_sensitive(opts.is_sensitive, content)
    from .encryption import encrypt_if_enabled
    stored_content = encrypt_if_enabled(
        content,
        is_sensitive=bool(is_sensitive_bit),
        tenant_id=tenant_id,
        user_id=uid,
    )
    # Store
    insert_sql = (
        "INSERT INTO memories (id, user_id, tenant_id, bank_id, category, scope, retention, "
        "content, content_hash, emotional_context, metrics, importance, activation_count, "
        "created_at, updated_at, tags, is_sensitive, sensitivity_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    insert_params = (
        memory_id,
        uid,
        tenant_id,
        BANK_ID,
        opts.category,
        memory.scope,
        memory.retention,
        stored_content,
        content_h,
        json.dumps(opts.emotional_context) if opts.emotional_context else None,
        json.dumps(opts.metrics) if opts.metrics else None,
        opts.importance,
        1,
        datetime.now(timezone.utc).isoformat(),
        datetime.now(timezone.utc).isoformat(),
        json.dumps(memory.tags),
        1 if is_sensitive_bit else 0,
        sensitivity_reason,
    )
    if _supports_execute_returning(conn):
        conn.execute_returning(insert_sql, insert_params).fetchone()
    else:
        conn.execute(insert_sql, insert_params)
    conn.commit()
    conn.close()
    # Create memory relationship if specified
    if opts.relation_type and opts.related_memory_id:
        store = get_memory_relationship_store()
        try:
            store.link_memories(
                source_memory_id=memory_id,
                target_memory_id=opts.related_memory_id,
                relationship_type=opts.relation_type,
                user_id=uid,
                options=LinkMemoriesOptions(
                    tenant_id=tenant_id,
                ),
            )
        except MemoryRelationshipError as exc:
            logger.warning(f"Failed to create memory relationship: {exc}")
    event_content = "[REDACTED - sensitive]" if is_sensitive_bit else content
    get_event_bus_with_stream().publish(memory_stored(memory_id=memory_id, content=event_content, actor=uid))
    get_hybrid_retriever().invalidate_tfidf_cache(uid, tenant_id)

    hook_ctx.memory_id = memory_id
    get_memory_hook_registry().emit_post(MemoryHookType.POST_STORE, hook_ctx)

    return f"Stored memory {memory_id}. Gate: {gate_result.decision}. Reason: {gate_result.reason}"


def _handle_memory_update(uid: str, tenant_id: str, options: MemoryAction) -> str:
    """Helper to handle memory updates."""
    if not options.memory_id or not options.updates:
        return "Error: memory_id and updates required for 'update' action"

    # ── PRE_UPDATE hook ────────────────────────────────────────────────
    hook_ctx = MemoryHookContext(
        action="update",
        memory_id=options.memory_id,
        user_id=uid,
        tenant_id=tenant_id,
        content=options.updates.content,
        category=options.updates.category,
        scope=options.updates.scope,
        retention=options.updates.retention,
    )
    for r in get_memory_hook_registry().emit_pre(MemoryHookType.PRE_UPDATE, hook_ctx):
        if r.abort:
            return f"Hook aborted update: {r.message}"

    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (options.memory_id, uid, tenant_id)
    ).fetchone()
    if not row:
        conn.close()
        return f"Memory {options.memory_id} not found."
    updates_list = []
    values = []
    if options.updates.content:
        create_version_snapshot(
            options.memory_id,
            {
                "content": row["content"],
                "tags": row["tags"],
                "emotional_context": row["emotional_context"],
                "metrics": row["metrics"],
                "version": row["version"] or 1,
                "tenant_id": row["tenant_id"],
            },
        )
        from .encryption import encrypt_if_enabled
        is_sensitive_bit, sensitivity_reason = resolve_is_sensitive(
            options.updates.is_sensitive, options.updates.content.strip()
        )
        stored_content = encrypt_if_enabled(
            options.updates.content.strip(),
            is_sensitive=bool(is_sensitive_bit),
            tenant_id=tenant_id,
            user_id=uid,
        )
        updates_list.extend(["content = ?", "version = ?"])
        values.extend([stored_content, (row["version"] or 1) + 1])
        updates_list.extend(["is_sensitive = ?", "sensitivity_reason = ?"])
        values.extend([1 if is_sensitive_bit else 0, sensitivity_reason])
    if options.updates.category:
        updates_list.append("category = ?")
        values.append(options.updates.category)
    if options.updates.scope:
        updates_list.append("scope = ?")
        values.append(options.updates.scope)
    if options.updates.retention:
        updates_list.append("retention = ?")
        values.append(options.updates.retention)
    if options.updates.tags:
        updates_list.append("tags = ?")
        values.append(json.dumps(options.updates.tags))
    if not updates_list:
        conn.close()
        return "No updates provided."
    updates_list.append("updated_at = ?")
    values.append(datetime.now(timezone.utc).isoformat())
    values.extend([options.memory_id, uid, tenant_id])
    update_sql = f"UPDATE memories SET {', '.join(updates_list)} WHERE id = ? AND user_id = ? AND tenant_id = ?"
    if _supports_execute_returning(conn):
        updated_row = conn.execute_returning(update_sql, values).fetchone()
    else:
        conn.execute(update_sql, values)
        updated_row = None
    conn.commit()
    conn.close()
    was_sensitive = bool(dict(row).get("is_sensitive", 0))
    old_evt = "[REDACTED - sensitive]" if was_sensitive else (row["content"] or "")
    effective_content = (updated_row or row)["content"] if (updated_row or row) else row["content"]
    new_evt = "[REDACTED - sensitive]" if was_sensitive else (effective_content or "")
    get_event_bus_with_stream().publish(
        memory_updated(
            memory_id=options.memory_id,
            old_content=old_evt,
            new_content=new_evt,
            actor=uid,
        )
    )
    get_hybrid_retriever().invalidate_tfidf_cache(uid, tenant_id)

    # ── POST_UPDATE hook ───────────────────────────────────────────────
    hook_ctx.old_content = row["content"]
    hook_ctx.content = effective_content or row["content"]
    get_memory_hook_registry().emit_post(MemoryHookType.POST_UPDATE, hook_ctx)
    # Create memory relationship if specified
    if options.updates.relation_type and options.updates.related_memory_id:
        store = get_memory_relationship_store()
        try:
            store.link_memories(
                source_memory_id=options.memory_id,
                target_memory_id=options.updates.related_memory_id,
                relationship_type=options.updates.relation_type,
                user_id=uid,
                tenant_id=tenant_id,
            )
        except MemoryRelationshipError as exc:
            logger.warning(f"Failed to create memory relationship: {exc}")
    return f"Updated memory {options.memory_id}"


def _handle_memory_delete(uid: str, tenant_id: str, memory_id: str | None) -> str:
    """Helper to handle memory deletion."""
    if not memory_id:
        return "Error: memory_id required for 'delete' action"

    # ── PRE_DELETE hook ────────────────────────────────────────────────
    hook_ctx = MemoryHookContext(
        action="delete",
        memory_id=memory_id,
        user_id=uid,
        tenant_id=tenant_id,
    )
    for r in get_memory_hook_registry().emit_pre(MemoryHookType.PRE_DELETE, hook_ctx):
        if r.abort:
            return f"Hook aborted delete: {r.message}"

    conn = get_db_connection()
    delete_sql = "DELETE FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?"
    delete_params = (memory_id, uid, tenant_id)
    if _supports_execute_returning(conn):
        deleted_row = conn.execute_returning(delete_sql, delete_params).fetchone()
        if not deleted_row:
            conn.close()
            return f"Memory {memory_id} not found."
    else:
        if not conn.execute(
            "SELECT id FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", delete_params
        ).fetchone():
            conn.close()
            return f"Memory {memory_id} not found."
        conn.execute(delete_sql, delete_params)

    get_event_bus_with_stream().publish(memory_deleted(memory_id=memory_id, actor=uid))
    conn.commit()
    conn.close()
    get_hybrid_retriever().invalidate_tfidf_cache(uid, tenant_id)

    # ── POST_DELETE hook ───────────────────────────────────────────────
    get_memory_hook_registry().emit_post(MemoryHookType.POST_DELETE, hook_ctx)

    return f"Deleted memory {memory_id}"


def _handle_memory_archive(uid: str, tenant_id: str, memory_id: str | None) -> str:
    """Helper to handle memory archiving."""
    if not memory_id:
        return "Error: memory_id required for 'archive' action"

    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (memory_id, uid, tenant_id)
    ).fetchone()

    if not row:
        conn.close()
        return f"Memory {memory_id} not found."
    row_dict = dict(row)
    if not row_dict.get("vector_id"):
        conn.close()
        return "Cannot archive memory without vector_id. Embed first."

    ms = get_memory_system()
    ghost = ms["linker"].to_ghost(
        MemoryObject(
            id=row_dict["id"],
            timestamp=row_dict["created_at"],
            scope=row_dict["scope"],
            retention=row_dict["retention"],
            content=row_dict["content"],
            tags=json.loads(row_dict["tags"]) or [],
            synthesized_from=json.loads(row_dict["synthesized_from"]) or [],
            is_ghost=bool(row_dict.get("is_ghost", 0)),
            vector_id=row_dict["vector_id"],
            gist=row_dict.get("gist"),
        )
    )
    archive_sql = (
        "UPDATE memories SET content = ?, is_ghost = 1, gist = ? WHERE id = ? AND user_id = ? AND tenant_id = ?"
    )
    archive_params = (ghost.content, ghost.gist, memory_id, uid, tenant_id)
    if _supports_execute_returning(conn):
        conn.execute_returning(archive_sql, archive_params).fetchone()
    else:
        conn.execute(archive_sql, archive_params)
    conn.commit()
    conn.close()
    return f"Archived memory {memory_id} to ghost node. Gist: {ghost.gist}"


@mcp.tool(output_schema=None)
def manage_memories(
    options: MemoryAction | None = None,
    user_id: str | None = None,
    action: Literal["store", "update", "delete", "archive"] | None = None,
    memory_id: str | None = None,
    content: str | None = None,
    store_options: MemoryOptions | None = None,
    updates: MemoryUpdateOptions | None = None,
    category: str | None = None,
    scope: str | None = None,
    retention: str | None = None,
    importance: float | None = None,
    tags: list[str] | None = None,
    is_sensitive: bool | None = None,
    relation_type: str | None = None,
    related_memory_id: str | None = None,
    emotional_context: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
) -> str:
    """
    Manage memory lifecycle: store, update, delete, or archive.

    Args:
        options: Action and parameters
        user_id: Optional user ID override
        action: Flat parameter for action (optional, fallback if options not provided)
        memory_id: Flat parameter for memory ID (optional, fallback if options not provided)
        content: Flat parameter for content (optional, fallback if options not provided)
        store_options: Flat parameter for store options (optional, fallback if options not provided)
        updates: Flat parameter for updates (optional, fallback if options not provided)
        category: Flat parameter for category label
        scope: Flat parameter for memory scope (session, arc, trait, fact)
        retention: Flat parameter for retention policy (ephemeral, short_term, long_term, permanent)
        importance: Flat parameter for importance score (0.0 to 1.0)
        tags: Flat parameter for tags list
        is_sensitive: Flat parameter for sensitivity override
        relation_type: Flat parameter for relationship type
        related_memory_id: Flat parameter for related memory ID
        emotional_context: Flat parameter for emotional metadata
        metrics: Flat parameter for empathy metrics
    """
    flat_store_kwargs: dict[str, Any] = {}
    if category is not None:
        flat_store_kwargs["category"] = category
    if scope is not None:
        flat_store_kwargs["scope"] = scope
    if retention is not None:
        flat_store_kwargs["retention"] = retention
    if importance is not None:
        flat_store_kwargs["importance"] = importance
    if emotional_context is not None:
        flat_store_kwargs["emotional_context"] = emotional_context
    if metrics is not None:
        flat_store_kwargs["metrics"] = metrics
    if relation_type is not None:
        flat_store_kwargs["relation_type"] = relation_type
    if related_memory_id is not None:
        flat_store_kwargs["related_memory_id"] = related_memory_id
    if is_sensitive is not None:
        flat_store_kwargs["is_sensitive"] = is_sensitive

    flat_update_kwargs: dict[str, Any] = {}
    if category is not None:
        flat_update_kwargs["category"] = category
    if scope is not None:
        flat_update_kwargs["scope"] = scope
    if retention is not None:
        flat_update_kwargs["retention"] = retention
    if tags is not None:
        flat_update_kwargs["tags"] = tags
    if is_sensitive is not None:
        flat_update_kwargs["is_sensitive"] = is_sensitive
    if relation_type is not None:
        flat_update_kwargs["relation_type"] = relation_type
    if related_memory_id is not None:
        flat_update_kwargs["related_memory_id"] = related_memory_id
    if content is not None:
        flat_update_kwargs["content"] = content

    if store_options is None and flat_store_kwargs:
        try:
            store_options = MemoryOptions(**flat_store_kwargs)
        except Exception:
            pass
    elif store_options is not None and flat_store_kwargs:
        for k, v in flat_store_kwargs.items():
            if getattr(store_options, k, None) is None or k in ("category", "scope", "retention"):
                setattr(store_options, k, v)

    if updates is None and flat_update_kwargs:
        try:
            updates = MemoryUpdateOptions(**flat_update_kwargs)
        except Exception:
            pass
    elif updates is not None and flat_update_kwargs:
        for k, v in flat_update_kwargs.items():
            if getattr(updates, k, None) is None:
                setattr(updates, k, v)

    if options is None:
        if action is None:
            return "Error: either 'options' or 'action' must be provided."
        options = MemoryAction(
            action=action,
            memory_id=memory_id,
            content=content,
            options=store_options,
            updates=updates,
        )
    else:
        if options.options is None and store_options is not None:
            options.options = store_options
        if options.updates is None and updates is not None:
            options.updates = updates

    uid = user_id or USER_ID
    tenant_id = get_current_account_id()

    if options.action == "store" and tenant_id == "default" and os.environ.get("PYTEST_CURRENT_TEST"):
        return (
            "Error: refusing to store to the 'default' tenant under pytest; set FORESIGHT_DB_PATH or use a test tenant"
        )

    if options.action == "store":
        return _handle_memory_store(uid, tenant_id, options)

    if options.action == "update":
        return _handle_memory_update(uid, tenant_id, options)

    if options.action == "delete":
        return _handle_memory_delete(uid, tenant_id, options.memory_id)

    if options.action == "archive":
        return _handle_memory_archive(uid, tenant_id, options.memory_id)

    return f"Unknown action: {options.action}"


@dataclass
class SearchTrace:
    """Structured trace of a retrieval operation for performance observability."""

    query: str
    latency_ms: float
    result_count: int
    total_candidates: int
    signal_counts: dict[str, Any]
    fast_path: int | str | None
    response_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "latency_ms": self.latency_ms,
            "result_count": self.result_count,
            "total_candidates": self.total_candidates,
            "signal_counts": self.signal_counts,
            "fast_path": self.fast_path,
            "response_bytes": self.response_bytes,
        }


def _trace_retrieval(
    query: str,
    uid: str,
    tenant_id: str,
    options: HybridSearchOptions,
) -> tuple[HybridSearchResult, SearchTrace]:
    """Run hybrid search and return (result, trace) pair."""
    t0 = time.perf_counter()
    retriever = get_hybrid_retriever()
    result = retriever.search(query, uid, options)
    latency_ms = (time.perf_counter() - t0) * 1000
    fast_path = result.signal_counts.get("fast_path")
    trace = SearchTrace(
        query=query,
        latency_ms=round(latency_ms, 2),
        result_count=len(result.results),
        total_candidates=result.total_candidates,
        signal_counts=result.signal_counts,
        fast_path=fast_path,
        response_bytes=0,
    )
    logger.debug(
        "search_memories trace: query=%r user=%s tenant=%s latency_ms=%.2f results=%d candidates=%d fast_path=%s signals=%s",
        query,
        uid,
        tenant_id,
        trace.latency_ms,
        trace.result_count,
        trace.total_candidates,
        fast_path,
        result.signal_counts,
    )
    return result, trace


@mcp.tool(output_schema=None)
def search_memories(
    options: SearchOptions | None = None,
    user_id: str | None = None,
    query_type: Literal["id", "keyword", "list", "semantic"] | None = None,
    query: str | None = None,
    memory_id: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    min_importance: float | None = None,
    use_hybrid: bool | None = None,
    use_cascade: bool | None = None,
    cascade_depth: int | None = None,
    cascade_limit: int | None = None,
    debug: bool | None = None,
    min_score: float | None = None,
    provider: str | None = None,
) -> str:
    """
    Unified search and retrieval for memories.
    Supports ID lookup, content keyword search, semantic vector search, and hybrid retrieval.

    Args:
        options: Search parameters (query_type, query, memory_id, limit, etc.)
        user_id: Optional user ID override
        query_type: Flat parameter for query type (optional)
        query: Flat parameter for query string (optional)
        memory_id: Flat parameter for memory ID (optional)
        limit: Flat parameter for limit (optional)
        offset: Flat parameter for offset (optional)
        min_importance: Flat parameter for min importance (optional)
        use_hybrid: Flat parameter for use hybrid (optional)
        use_cascade: Flat parameter for use cascade (optional)
        cascade_depth: Flat parameter for cascade depth (optional)
        cascade_limit: Flat parameter for cascade limit (optional)
        debug: Flat parameter for debug mode (optional)
        min_score: Flat parameter for min semantic similarity score (optional)
        provider: Flat parameter for embedder provider name (optional)
    """
    if options is None:
        kwargs = {}
        if query_type is not None:
            kwargs["query_type"] = query_type
        if query is not None:
            kwargs["query"] = query
        if memory_id is not None:
            kwargs["memory_id"] = memory_id
        if limit is not None:
            kwargs["limit"] = limit
        if offset is not None:
            kwargs["offset"] = offset
        if min_importance is not None:
            kwargs["min_importance"] = min_importance
        if use_hybrid is not None:
            kwargs["use_hybrid"] = use_hybrid
        if use_cascade is not None:
            kwargs["use_cascade"] = use_cascade
        if cascade_depth is not None:
            kwargs["cascade_depth"] = cascade_depth
        if cascade_limit is not None:
            kwargs["cascade_limit"] = cascade_limit
        if debug is not None:
            kwargs["debug"] = debug
        if min_score is not None:
            kwargs["min_score"] = min_score
        if provider is not None:
            kwargs["provider"] = provider
        options = SearchOptions(**kwargs)

    uid = user_id or USER_ID
    tenant_id = get_current_account_id()

    # ── PRE_RETRIEVE hook ──────────────────────────────────────────────
    hook_ctx = MemoryHookContext(
        action="retrieve",
        user_id=uid,
        tenant_id=tenant_id,
        query=options.query,
    )
    for r in get_memory_hook_registry().emit_pre(MemoryHookType.PRE_RETRIEVE, hook_ctx):
        if r.abort:
            return f"Hook aborted retrieval: {r.message}"
        if r.modified_context:
            query_override = r.modified_context.get("query", options.query)
            if query_override:
                options.query = query_override

    # 0. Semantic vector search
    if options.query_type == "semantic":
        if not options.query:
            return "Error: query is required for semantic search."
        prov = options.provider or _SEMANTIC_DEFAULT_PROVIDER
        try:
            store = get_semantic_search(provider=prov)
            result = store.search(
                query=options.query,
                user_id=uid,
                options=SemanticSearchOptions(
                    limit=options.limit,
                    min_score=options.min_score,
                ),
            )
        except _SemanticSearchError as exc:
            return f"Error: {exc}"
        # Reinforce surfaced memories (closes decay loop).
        _auto_reinforce_batch(
            [m.memory_id for m in result.matches], uid, tenant_id
        )
        get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
        return json.dumps(result.to_dict(), indent=2)

    # 1. Direct ID lookup
    if options.query_type == "id" or options.memory_id:
        mid = options.memory_id or options.query
        if not mid:
            return "Error: memory_id or query (as ID) required for id lookup."
        conn = get_db_connection()
        row = conn.execute(
            "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (mid, uid, tenant_id)
        ).fetchone()
        conn.close()

        if not row:
            # ── POST_RETRIEVE hook (no results) ────────────────────────
            get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
            return f"Memory {mid} not found."

        # Emit event
        event_bus = get_event_bus_with_stream()
        event_bus.publish(memory_retrieved(memory_id=mid, query_context="", actor=uid))

        from .encryption import decrypt_if_encrypted
        tags = json.loads(row["tags"])
        decrypted = decrypt_if_encrypted(row["content"], tenant_id=tenant_id, user_id=uid)
        result = f"[{row['id']}] ({row['scope']}/{row['retention']})\n"
        result += f"Content: {decrypted}\n"
        result += f"Tags: {', '.join(tags) if tags else 'none'}\n"
        if row["is_ghost"]:
            result += "[GHOST NODE - Content archived]"

        # ── POST_RETRIEVE hook (ID lookup) ─────────────────────────────
        hook_ctx.memory_id = mid
        get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
        return result

    # 2. Hybrid search if enabled and query provided
    if options.use_hybrid and options.query:
        try:
            hybrid_result, trace = _trace_retrieval(
                options.query,
                uid,
                tenant_id,
                HybridSearchOptions(
                    tenant_id=tenant_id,
                    limit=options.limit,
                    min_importance=options.min_importance,
                    use_keyword=True,
                    use_tfidf_cosine=True,
                    use_semantic=None,
                    use_graph=True,
                    use_temporal=True,
                ),
            )
            if options.debug:
                return json.dumps(trace.to_dict(), indent=2)
            if hybrid_result.results:
                results = []
                for r in hybrid_result.results:
                    signals = ", ".join(r.source_signals) if r.source_signals else "hybrid"
                    results.append(
                        f"- [{r.memory_id}] {r.content[:100]}... (score={r.combined_score:.3f}, signals={signals})"
                    )
                # Reinforce surfaced memories (closes decay loop).
                _auto_reinforce_batch(
                    [r.memory_id for r in hybrid_result.results], uid, tenant_id
                )
                # ── POST_RETRIEVE hook (hybrid search) ─────────────────
                get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
                return f"Found {len(results)} memories (hybrid search):\n" + "\n".join(results)
        except Exception as e:
            logger.debug(f"Hybrid search failed: {e}")

    # 3. Fallback to basic list/keyword search
    conn = get_db_connection()
    if options.query:
        escaped = options.query.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        query_sql = (
            "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? AND content LIKE ? ESCAPE '!' LIMIT ? OFFSET ?"
        )
        params = (uid, tenant_id, f"%{escaped}%", options.limit, options.offset)
    else:
        query_sql = (
            "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        params = (uid, tenant_id, options.limit, options.offset)

    rows = conn.execute(query_sql, params).fetchall()
    conn.close()

    if not rows:
        # ── POST_RETRIEVE hook (no results) ────────────────────────────
        get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
        return "No memories found."

    from .encryption import decrypt_if_encrypted
    results = [
        f"- [{r['id']}] ({r['scope']}/{r['retention']}) {decrypt_if_encrypted(r['content'], tenant_id=tenant_id, user_id=uid)[:80]}..."
        for r in rows
    ]
    # Reinforce surfaced memories (closes decay loop).
    _auto_reinforce_batch([r["id"] for r in rows], uid, tenant_id)
    # ── POST_RETRIEVE hook (fallback) ─────────────────────────────────
    get_memory_hook_registry().emit_post(MemoryHookType.POST_RETRIEVE, hook_ctx)
    return f"Memories ({len(results)} found):\n" + "\n".join(results)


# =============================================================================
# Memory Versioning Tools
# =============================================================================


def _handle_version_rollback(uid: str, tenant_id: str, options: VersionAction) -> str:
    """Helper to handle memory version rollback."""
    if options.to_version is None:
        return "Error: to_version required for rollback"

    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (options.memory_id, uid, tenant_id)
    ).fetchone()
    if not row:
        conn.close()
        return f"Memory {options.memory_id} not found."

    version_row = conn.execute(
        "SELECT * FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (options.memory_id, options.to_version, tenant_id),
    ).fetchone()
    if not version_row:
        conn.close()
        return f"Version {options.to_version} not found for memory {options.memory_id}."

    version_id = hashlib.sha256(
        f"{options.memory_id}{row['version']}{datetime.now(timezone.utc).isoformat()}".encode()
    ).hexdigest()[:16]
    conn.execute(
        "INSERT INTO memory_versions (id, memory_id, tenant_id, content, version, created_at, tags, emotional_context, metrics, rollback_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            version_id,
            options.memory_id,
            tenant_id,
            row["content"],
            row["version"] or 1,
            datetime.now(timezone.utc).isoformat(),
            row["tags"],
            row["emotional_context"],
            row["metrics"],
            None,
        ),
    )
    new_version = options.to_version + 1
    conn.execute(
        "UPDATE memories SET content = ?, tags = ?, emotional_context = ?, metrics = ?, version = ?, updated_at = ? WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (
            version_row["content"],
            version_row["tags"],
            version_row["emotional_context"],
            version_row["metrics"],
            new_version,
            datetime.now(timezone.utc).isoformat(),
            options.memory_id,
            uid,
            tenant_id,
        ),
    )
    conn.commit()
    conn.close()
    was_sensitive = bool(dict(row).get("is_sensitive", 0))
    old_evt = "[REDACTED - sensitive]" if was_sensitive else (row["content"] or "")
    new_evt = "[REDACTED - sensitive]" if was_sensitive else (version_row["content"] or "")
    get_event_bus_with_stream().publish(
        memory_updated(memory_id=options.memory_id, old_content=old_evt, new_content=new_evt, actor=uid)
    )
    return f"Rolled back memory {options.memory_id} to version {options.to_version} (now at {new_version})"


def _handle_version_list(uid: str, tenant_id: str, options: VersionAction) -> str:
    """Helper to list all versions of a memory."""
    _ = tenant_id  # resolved internally by get_memory_versions
    return get_memory_versions(options.memory_id, user_id=uid)


def _handle_version_diff(uid: str, tenant_id: str, options: VersionAction) -> str:
    """Helper to handle memory version diff."""
    if options.version1 is None or options.version2 is None:
        return "Error: version1 and version2 required for diff"

    conn = get_db_connection()
    if not conn.execute(
        "SELECT id FROM memories WHERE id = ? AND user_id = ? AND tenant_id = ?", (options.memory_id, uid, tenant_id)
    ).fetchone():
        conn.close()
        return f"Memory {options.memory_id} not found."

    v1 = conn.execute(
        "SELECT content, created_at FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (options.memory_id, options.version1, tenant_id),
    ).fetchone()
    v2 = conn.execute(
        "SELECT content, created_at FROM memory_versions WHERE memory_id = ? AND version = ? AND tenant_id = ?",
        (options.memory_id, options.version2, tenant_id),
    ).fetchone()
    conn.close()

    if not v1:
        return f"Version {options.version1} not found."
    if not v2:
        return f"Version {options.version2} not found."

    res = [
        f"Diff for {options.memory_id}:",
        f"V{options.version1}: {v1['content'][:50]}...",
        f"V{options.version2}: {v2['content'][:50]}...",
    ]
    res.append("Changed." if v1["content"] != v2["content"] else "Identical.")
    return "\n".join(res)


@mcp.tool(output_schema=None)
def manage_memory_versions(
    options: VersionAction | None = None,
    user_id: str | None = None,
    action: Literal["list", "diff", "rollback"] | None = None,
    memory_id: str | None = None,
    version1: int | None = None,
    version2: int | None = None,
    to_version: int | None = None,
) -> str:
    """
    Manage memory versioning: diff or rollback.

    Args:
        options: Action and parameters
        user_id: Optional user ID override
        action: Flat parameter for action (optional, fallback if options not provided)
        memory_id: Flat parameter for memory ID (optional, fallback if options not provided)
        version1: Flat parameter for first version for diff
        version2: Flat parameter for second version for diff
        to_version: Flat parameter for target version for rollback
    """
    if options is None:
        if action is None or memory_id is None:
            return "Error: either 'options' or ('action' and 'memory_id') must be provided."
        options = VersionAction(
            action=action,
            memory_id=memory_id,
            version1=version1,
            version2=version2,
            to_version=to_version,
        )

    uid = user_id or USER_ID
    tenant_id = get_current_account_id()

    if options.action == "list":
        return _handle_version_list(uid, tenant_id, options)

    if options.action == "rollback":
        return _handle_version_rollback(uid, tenant_id, options)

    if options.action == "diff":
        return _handle_version_diff(uid, tenant_id, options)

    return f"Unknown action: {options.action}"


# =============================================================================
# Context Block Tools
# =============================================================================


def _tool_response(*, ok: bool, action: str, **payload: Any) -> str:
    """Return a stable JSON envelope for tool responses."""
    body: dict[str, Any] = {"ok": ok, "action": action}
    body.update(payload)
    return json.dumps(body, indent=2)


def _tool_error(action: str, message: str, **payload: Any) -> str:
    """Return a stable JSON envelope for tool failures."""
    return _tool_response(ok=False, action=action, error={"message": message}, **payload)


def _handle_context_block_list(agent) -> str:
    """Helper for context block list action."""
    blocks = agent.get_all_blocks()
    return _tool_response(ok=True, action="list", blocks=blocks)


def _handle_context_block_get(agent, label: str) -> str:
    """Helper for context block get action."""
    content = agent.get_block(label)
    if content is not None:
        return _tool_response(ok=True, action="get", label=label, content=content)
    return _tool_error("get", f"Block '{label}' not found.", label=label)


def _handle_context_block_update(agent, label: str, content: str | None) -> str:
    """Helper for context block update action."""
    if content is None:
        return _tool_error("update", "'content' is required for update.", label=label)
    try:
        agent.update_block(label, content)
    except ValueError as exc:
        return _tool_error("update", str(exc), label=label)
    return _tool_response(ok=True, action="update", label=label, message=f"Updated block '{label}'")


@mcp.tool(output_schema=None)
def manage_context_blocks(
    options: ContextBlockAction | None = None,
    user_id: str | None = None,
    action: Literal["list", "get", "update", "reset", "clear"] | None = None,
    label: str | None = None,
    block_name: str | None = None,
    block_type: str | None = None,
    name: str | None = None,
    block: str | None = None,
    block_id: str | None = None,
    key: str | None = None,
    content: str | None = None,
) -> str:
    """
    Manage Foresight context blocks (guidance, preferences, context).

    Args:
        options: Action and parameters (list, get, update, reset, clear)
        user_id: Optional user ID override
        action: Flat parameter for action (optional, fallback if options not provided)
        label: Flat parameter for block label (optional, fallback if options not provided)
        block_name: Alias for label (optional)
        block_type: Alias for label (optional)
        name: Alias for label (optional)
        block: Alias for label (optional)
        block_id: Alias for label (optional)
        key: Alias for label (optional)
        content: Flat parameter for block content (optional, fallback if options not provided)
    """
    resolved_label = label or block_name or block_type or name or block or block_id or key
    if options is None:
        if action is None:
            return "Error: either 'options' or 'action' must be provided."
        options = ContextBlockAction(action=action, label=resolved_label, content=content)
    elif not options.label and resolved_label:
        options.label = resolved_label

    uid = user_id or USER_ID
    tenant_id = get_current_account_id()
    agent = get_context_block_agent(uid, tenant_id)

    if options.action == "list":
        res = _handle_context_block_list(agent)
    elif not options.label:
        res = _tool_error(options.action, "'label' is required for this action.")
    elif options.action == "get":
        res = _handle_context_block_get(agent, options.label)
    elif options.action == "update":
        res = _handle_context_block_update(agent, options.label, options.content)
    elif options.action in ("reset", "clear"):
        try:
            if options.action == "reset":
                agent.reset_block(options.label)
            else:
                agent.clear_block(options.label)
            message = (
                f"Reset block '{options.label}' to default"
                if options.action == "reset"
                else f"Cleared block '{options.label}'"
            )
            res = _tool_response(
                ok=True,
                action=options.action,
                label=options.label,
                message=message,
            )
        except ValueError as exc:
            res = _tool_error(options.action, str(exc), label=options.label)
    else:
        res = _tool_error(options.action, f"Unsupported action: {options.action}")

    return res


def manage_subconscious(options: SubconsciousAction, user_id: str | None = None) -> str:
    """Legacy alias for manage_context_blocks()."""
    return manage_context_blocks(ContextBlockAction(**options.model_dump()), user_id=user_id)


def _bridge_context_blocks_to_memories(agent, uid: str) -> int:
    """Bridge extracted context block items into the memory store.

    Reads the most recent items from user_preferences, pending_items,
    and session_patterns blocks and stores each as a deduplicated memory.

    Returns the number of new memories stored.
    """
    stored = 0
    now = datetime.now(timezone.utc).isoformat()
    block_map = [
        (USER_PREFERENCES, "preference"),
        (PENDING_ITEMS, "pending"),
        (SESSION_PATTERNS, "pattern"),
    ]

    with get_db_connection() as conn:
        for block_name, category in block_map:
            block = agent.state.get_block(block_name)
            if not block or block.is_empty():
                continue

            lines = [ln.strip() for ln in block.content.splitlines() if ln.strip()]
            recent = lines[-5:]

            for line in recent:
                content = f"[{block_name}] {line}"
                tenant_id = get_current_account_id()
                content_h = _content_hash(content)
                existing = conn.execute(
                    "SELECT id, activation_count FROM memories "
                    "WHERE user_id = ? AND tenant_id = ? AND content_hash = ? AND is_ghost = 0 "
                    "ORDER BY created_at DESC LIMIT 1",
                    (uid, tenant_id, content_h),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE memories SET activation_count = activation_count + 1, updated_at = ? "
                        "WHERE id = ? AND user_id = ? AND tenant_id = ?",
                        (now, existing["id"], uid, tenant_id),
                    )
                    continue

                mid = hashlib.sha256(f"{content}{now}".encode()).hexdigest()[:16]
                is_sensitive_bit, sensitivity_reason = resolve_is_sensitive(None, content)
                conn.execute(
                    "INSERT INTO memories "
                    "(id, content, content_hash, scope, retention, category, user_id, bank_id, tenant_id, "
                    "created_at, updated_at, tags, emotional_context, metrics, "
                    "is_ghost, synthesized_from, is_sensitive, sensitivity_reason) "
                    "VALUES (?, ?, ?, 'arc', 'long_term', ?, ?, ?, ?, ?, ?, '[]', '{}', '{}', 0, '[]', ?, ?) "
                    "ON CONFLICT (id) DO NOTHING",
                    (
                        mid,
                        content,
                        content_h,
                        category,
                        uid,
                        BANK_ID,
                        tenant_id,
                        now,
                        now,
                        1 if is_sensitive_bit else 0,
                        sensitivity_reason,
                    ),
                )
                stored += 1

    return stored


# Map capture-pipeline categories to context-block labels
_CAPTURE_TO_BLOCK = {
    "preference": USER_PREFERENCES,
    "pending_item": PENDING_ITEMS,
    "pattern": SESSION_PATTERNS,
    "decision": PROJECT_CONTEXT,
}


def _bridge_capture_memories_to_blocks(
    agent,
    stored_items: list[tuple[str, str]],
) -> int:
    """Sync capture-pipeline memories back into context blocks.

    *stored_items* is a list of ``(category, content)`` tuples from
    ``CaptureStats.stored_items``.  Each item is appended to the matching
    block if it isn't already present, keeping blocks populated even when
    the capture pipeline finds items that the transcript processor missed.

    Returns the number of items appended.
    """
    appended = 0
    modified_labels: set[str] = set()
    for category, content in stored_items:
        label = _CAPTURE_TO_BLOCK.get(category)
        if not label:
            continue
        content = content.strip()
        if not content:
            continue
        block = agent.state.get_block(label)
        existing_lines = (
            {ln.strip() for ln in block.content.splitlines() if ln.strip()} if block and not block.is_empty() else set()
        )
        if content in existing_lines:
            continue
        agent.state.append_to_block(label, content)
        modified_labels.add(label)
        appended += 1
    for label in modified_labels:
        agent._persist_block(label)
    return appended


def _bridge_transcript_entities(messages: list[dict], uid: str) -> int:
    """Run entity extraction on transcript content and persist found entities.

    Returns the number of entities stored.
    """

    user_content = " ".join(msg.get("content", "") for msg in messages if msg.get("role") == "user")[:3000]

    if not user_content.strip():
        return 0

    extractor = get_entity_extractor()
    result = _run_async(extractor.extract(user_content))

    if not result.entities:
        return 0

    store = get_graph_store()
    store.process_extraction_result(result, uid)

    return len(result.entities)


def _detect_git_root() -> str | None:
    """Return the current git repository root, or None if not in a git repo.

    Used by process_session_transcript to automatically fill project_path
    without the caller having to discover or pass it.
    """
    import subprocess as _sp

    try:
        r = _sp.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if r.returncode == 0:
            return r.stdout.strip() or None
    except Exception:
        pass
    return None


@mcp.tool(output_schema=None)
def process_session_transcript(
    session_id: str, messages: list[dict], project_path: str | None = None, user_id: str | None = None
) -> str:
    """
    Process a session transcript and extract memories.

    Args:
        session_id: Unique session identifier
        messages: List of message dicts with role/content
        project_path: Optional project path for context
        user_id: Optional user ID override

    Returns:
        Confirmation message
    """
    uid = user_id or USER_ID
    tenant_id = get_current_account_id()

    # Auto-detect the git repository root as project path when not supplied.
    # Improves extraction quality at zero cost to the caller.
    if project_path is None:
        project_path = _detect_git_root()

    agent = get_context_block_agent(uid, tenant_id)

    _run_async(agent.process_transcript(session_id=session_id, messages=messages, project_path=project_path))

    _bridge_context_blocks_to_memories(agent, uid)
    _bridge_transcript_entities(messages, uid)

    pipeline = get_capture_pipeline()
    stats = pipeline.run(session_id=session_id, messages=messages, user_id=uid, tenant_id=tenant_id)

    # Sync capture-pipeline memories back into context blocks
    if stats.stored_items:
        bridged = _bridge_capture_memories_to_blocks(agent, stats.stored_items)
        if bridged:
            _bridge_context_blocks_to_memories(agent, uid)

    return f"Processed transcript for session {session_id} ({stats.stored} new memories)"


@mcp.tool(output_schema=None)
def capture_triggered_memories(
    text: str,
    user_id: str | None = None,
) -> str:
    uid = user_id or USER_ID
    matches = extract_triggered_memories(text, triggers=DEFAULT_TRIGGERS)

    if not matches:
        return json.dumps(
            {
                "ok": True,
                "match_count": 0,
                "stored_count": 0,
                "results": [],
                "message": "No phrase triggers detected.",
            },
            indent=2,
        )

    results: list[dict[str, Any]] = []
    stored_count = 0

    for match in matches:
        options = MemoryAction(
            action="store",
            content=match.content,
            options=MemoryOptions(
                category=match.metadata.get("category", "fact"),
                scope=match.metadata.get("scope", "session"),
                retention=match.metadata.get("retention", "short_term"),
                importance=match.metadata.get("importance", 0.5),
            ),
        )
        store_result = manage_memories(options, user_id=uid)
        stored_count += 1
        results.append(
            {
                "trigger": match.trigger,
                "content": match.content,
                "position": match.position,
                "metadata": match.metadata,
                "store_result": store_result,
            }
        )

    return json.dumps(
        {
            "ok": True,
            "match_count": len(matches),
            "stored_count": stored_count,
            "results": results,
        },
        indent=2,
    )


# =============================================================================
# Curation Run Tools
# =============================================================================


def _row_to_curation_run(row: RowLike | None) -> dict[str, Any] | None:
    """Convert a curation run row into a JSON-friendly dict."""
    if row is None:
        return None
    summary = row["summary_json"] or "{}"
    error = row["error_json"] or "{}"
    return {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "source_bank_id": row["source_bank_id"],
        "output_bank_id": row["output_bank_id"],
        "policy_mode": row["policy_mode"],
        "tool_access": row["tool_access"],
        "output_mode": row["output_mode"],
        "status": row["status"],
        "instructions": row["instructions"],
        "summary": json.loads(summary),
        "error": json.loads(error),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "archived_at": row["archived_at"],
    }


def _curation_run_output_bank(
    run_id: str, _source_bank_id: str, output_mode: str, requested_output_bank: str | None
) -> str:
    """Resolve the effective output bank for a curation run."""
    if output_mode == "in_place":
        return f"curation:stage:{run_id}"
    return requested_output_bank or f"curation:{run_id}"


def _curation_archive_bank(run_id: str, source_bank_id: str) -> str:
    """Return the archive bank used when an in-place run replaces source rows."""
    return f"{source_bank_id}:archived:{run_id}"


def _fetch_curation_run(uid: str, tenant_id: str, run_id: str) -> RowLike | None:
    """Load a curation run row for a user."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT * FROM curation_runs WHERE id = ? AND user_id = ? AND tenant_id = ?",
        (run_id, uid, tenant_id),
    ).fetchone()
    conn.close()
    return row


def _curation_payload_from_row(row: RowLike) -> dict[str, Any]:
    """Rebuild a worker payload from a curation run row."""
    row_dict = dict(row)
    transcript_bundle_json = row_dict.get("transcript_bundle_json")
    return {
        "tenant_id": row_dict["tenant_id"],
        "user_id": row_dict["user_id"],
        "source_bank_id": row_dict["source_bank_id"],
        "output_bank_id": row_dict["output_bank_id"],
        "policy_mode": row_dict["policy_mode"],
        "tool_access": row_dict["tool_access"],
        "output_mode": row_dict["output_mode"],
        "instructions": row_dict["instructions"],
        "run_clustering": row_dict.get("run_clustering", False),
        "transcript_bundle": json.loads(transcript_bundle_json) if transcript_bundle_json else None,
        "session_id": row_dict.get("session_id"),
        "project_path": row_dict.get("project_path"),
    }


def _update_curation_run(
    run_id: str,
    tenant_id: str,
    *,
    status: str | None = None,
    summary: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    archived_at: str | None = None,
) -> None:
    """Update mutable curation run fields."""
    updates: list[str] = []
    values: list[Any] = []
    if status is not None:
        updates.append("status = ?")
        values.append(status)
    if summary is not None:
        updates.append("summary_json = ?")
        values.append(json.dumps(summary))
    if error is not None:
        updates.append("error_json = ?")
        values.append(json.dumps(error))
    if started_at is not None:
        updates.append("started_at = ?")
        values.append(started_at)
    if ended_at is not None:
        updates.append("ended_at = ?")
        values.append(ended_at)
    if archived_at is not None:
        updates.append("archived_at = ?")
        values.append(archived_at)
    if not updates:
        return
    conn = get_db_connection()
    conn.execute(
        f"UPDATE curation_runs SET {', '.join(updates)} WHERE id = ? AND tenant_id = ?",
        (*values, run_id, tenant_id),
    )
    conn.commit()
    conn.close()


def _claim_curation_run(run_id: str, tenant_id: str, started_at: str) -> bool:
    """Atomically claim a pending curation run for execution."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """UPDATE curation_runs
            SET status = 'running', started_at = ?, ended_at = NULL, error_json = '{}'
            WHERE id = ? AND tenant_id = ? AND status = 'pending'""",
            (started_at, run_id, tenant_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def _publish_curation_status(
    run_id: str,
    status: str,
    actor: str,
    **payload: Any,
) -> None:
    """Publish a curation lifecycle event."""
    get_event_bus_with_stream().publish(curation_status_changed(run_id, status, payload=payload, actor=actor))


def _get_curation_cancel_event(run_id: str) -> threading.Event:
    """Get or create the cancellation event for a curation run."""
    with _CURATION_WORKERS_LOCK:
        event = _CURATION_CANCEL_SIGNALS.get(run_id)
        if event is None:
            event = threading.Event()
            _CURATION_CANCEL_SIGNALS[run_id] = event
        return event


def _is_run_canceled(uid: str, tenant_id: str, run_id: str) -> bool:
    """Check whether a run has been canceled while background work is in progress."""
    if _get_curation_cancel_event(run_id).is_set():
        return True
    row = _fetch_curation_run(uid, tenant_id, run_id)
    return bool(row and row["status"] == "canceled")


def _raise_if_run_canceled(uid: str, tenant_id: str, run_id: str) -> None:
    """Abort background work when the run has been canceled."""
    if _is_run_canceled(uid, tenant_id, run_id):
        raise CurationError(f"Curation run {run_id} was canceled")


def _delete_existing_curation_outputs(uid: str, tenant_id: str, bank_id: str, run_id: str) -> None:
    """Remove stale outputs for a rerun or resumed run before writing fresh results."""
    conn = get_db_connection()
    try:
        conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND tenant_id = ? AND bank_id = ? AND tags LIKE ?",
            (uid, tenant_id, bank_id, f'%"curation_run:{run_id}"%'),
        )
        conn.commit()
    finally:
        conn.close()


def _promote_in_place_curation(
    uid: str,
    tenant_id: str,
    run_id: str,
    source_bank_id: str,
    staging_bank_id: str,
    source_rows: list[RowLike],
    staged_ids: list[str],
    *,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Atomically archive original source rows and promote staged rows into the source bank."""
    archive_bank_id = _curation_archive_bank(run_id, source_bank_id)
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        if cancel_event and cancel_event.is_set():
            raise CurationError("Curation canceled before promotion started")
        if _is_run_canceled(uid, tenant_id, run_id):
            raise CurationError("Curation canceled before promotion started")
        conn.execute("BEGIN")
        if cancel_event and cancel_event.is_set():
            raise CurationError("Curation canceled before promotion committed")
        if source_rows:
            placeholders = ",".join("?" for _ in source_rows)
            conn.execute(
                f"UPDATE memories SET bank_id = ?, updated_at = ? WHERE user_id = ? AND tenant_id = ? AND id IN ({placeholders})",
                (archive_bank_id, now, uid, tenant_id, *(row["id"] for row in source_rows)),
            )
        if staged_ids:
            placeholders = ",".join("?" for _ in staged_ids)
            conn.execute(
                f"UPDATE memories SET bank_id = ?, updated_at = ? WHERE user_id = ? AND tenant_id = ? AND id IN ({placeholders})",
                (source_bank_id, now, uid, tenant_id, *staged_ids),
            )
        if cancel_event and cancel_event.is_set():
            raise CurationError("Curation canceled before promotion committed")
        if _is_run_canceled(uid, tenant_id, run_id):
            raise CurationError("Curation canceled before promotion committed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "archive_bank_id": archive_bank_id,
        "promoted_memory_count": len(staged_ids),
        "archived_memory_count": len(source_rows),
        "staging_bank_id": staging_bank_id,
    }


def _restore_in_place_curation(
    uid: str,
    tenant_id: str,
    source_bank_id: str,
    staging_bank_id: str,
    source_rows: list[RowLike],
    staged_ids: list[str],
) -> None:
    """Restore source/staging banks if cancellation lands after promotion."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    try:
        conn.execute("BEGIN")
        if source_rows:
            placeholders = ",".join("?" for _ in source_rows)
            conn.execute(
                f"UPDATE memories SET bank_id = ?, updated_at = ? WHERE user_id = ? AND tenant_id = ? AND id IN ({placeholders})",
                (source_bank_id, now, uid, tenant_id, *(row["id"] for row in source_rows)),
            )
        if staged_ids:
            placeholders = ",".join("?" for _ in staged_ids)
            conn.execute(
                f"UPDATE memories SET bank_id = ?, updated_at = ? WHERE user_id = ? AND tenant_id = ? AND id IN ({placeholders})",
                (staging_bank_id, now, uid, tenant_id, *staged_ids),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _load_source_bank_rows(uid: str, tenant_id: str, bank_id: str, limit: int = 100) -> list[RowLike]:
    """Load source-bank memories for curation."""
    conn = get_db_connection()
    rows = conn.execute(
        """SELECT id, content, category, importance, strength_trend,
        activation_count, tags, emotional_context, created_at, scope, retention
        FROM memories
        WHERE user_id = ? AND tenant_id = ? AND bank_id = ? AND is_ghost = 0
        ORDER BY importance DESC, created_at DESC
        LIMIT ?""",
        (uid, tenant_id, bank_id, limit),
    ).fetchall()
    conn.close()
    return rows


def _build_synthesis_snapshot(rows: list[RowLike], uid: str) -> dict[str, Any] | None:
    """Run the enhanced synthesizer when enough source memories exist."""
    if len(rows) < 5:
        return None
    memories: list[MemoryObject] = []
    for row in rows:
        emo = json.loads(row["emotional_context"]) if row["emotional_context"] else {}
        memories.append(
            MemoryObject(
                id=row["id"],
                timestamp=row["created_at"],
                scope=row["scope"] or "arc",
                retention=row["retention"] or "long_term",
                content=row["content"],
                tags=json.loads(row["tags"]) if row["tags"] else [],
                emotional_context=EmotionalMetadata(intensity=emo.get("intensity", 0.5)) if emo else None,
            )
        )
    result = _run_async(get_enhanced_synthesizer().synthesize(memories, user_id=uid))
    return result.to_dict() if result else None


def _build_reflection_snapshot(rows: list[RowLike], uid: str, tenant_id: str) -> dict[str, Any] | None:
    """Build a non-persisting reflection summary from existing reflection primitives."""
    if not rows:
        return None
    engine = get_reflection_engine()
    conn = get_db_connection()
    try:
        trend_summary = engine._build_trend_summary(rows)
        entity_summary = engine._build_entity_summary(conn, uid, tenant_id)
        insights = [insight.to_dict() for insight in engine._generate_insights(rows, trend_summary, entity_summary)[:5]]
        return {
            "trend_summary": trend_summary,
            "entity_summary": entity_summary,
            "insights": insights,
        }
    finally:
        conn.close()


def _context_block_snapshot(uid: str, tenant_id: str) -> list[dict[str, Any]]:
    """Return non-empty context blocks for summary generation."""
    return cast(list[dict[str, Any]], get_context_block_agent(uid, tenant_id).get_all_blocks())


def _make_curated_entries(
    run: dict[str, Any],
    source_rows: list[RowLike],
    block_snapshot: list[dict[str, Any]],
    synthesis: dict[str, Any] | None,
    reflection: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Build curated output entries based on the configured policy mode."""
    tags = [
        f"curation_run:{run['id']}",
        f"source_bank:{run['source_bank_id']}",
        f"policy:{run['policy_mode']}",
    ]
    entries: list[dict[str, Any]] = []

    context_labels = [block["label"] for block in block_snapshot if block.get("content")]

    summary_lines = [
        f"Foresight Curator run {run['id']} completed in {run['output_mode']} mode.",
        f"Source bank: {run['source_bank_id']}",
        f"Output bank: {run['output_bank_id']}",
        f"Policy: {run['policy_mode']}",
        f"Tool access: {run['tool_access']}",
        f"Source memories considered: {len(source_rows)}",
    ]
    if run.get("instructions"):
        summary_lines.append(f"Instructions: {run['instructions']}")
    if context_labels:
        summary_lines.append(f"Context blocks considered: {', '.join(context_labels[:5])}")
    if synthesis:
        summary_lines.append(f"Synthesis contradictions: {len(synthesis.get('contradictions', []))}")
        summary_lines.append(f"Synthesis insights: {len(synthesis.get('insights', []))}")
    if reflection:
        summary_lines.append(f"Reflection overall trend: {reflection['trend_summary'].get('overall', 'stable')}")
        summary_lines.append(f"Reflection insights: {len(reflection.get('insights', []))}")

    entries.append(
        {
            "content": "\n".join(summary_lines),
            "category": "curation_summary",
            "scope": "arc",
            "retention": "long_term",
            "tags": [*tags, "summary"],
        }
    )

    if run["policy_mode"] == "preserve":
        for row in source_rows[:10]:
            entries.append(
                {
                    "content": row["content"],
                    "category": row["category"] or "curated_memory",
                    "scope": row["scope"] or "arc",
                    "retention": row["retention"] or "long_term",
                    "tags": [*tags, "preserved"],
                }
            )
    elif run["policy_mode"] == "rebalance":
        for row in source_rows[:5]:
            entries.append(
                {
                    "content": f"[Rebalanced] {row['content']}",
                    "category": row["category"] or "curated_memory",
                    "scope": row["scope"] or "arc",
                    "retention": row["retention"] or "long_term",
                    "tags": [*tags, "rebalanced"],
                }
            )
        if synthesis and synthesis.get("insights"):
            entries.append(
                {
                    "content": "\n".join(f"- {insight['summary']}" for insight in synthesis["insights"][:5]),
                    "category": "curation_insight",
                    "scope": "arc",
                    "retention": "long_term",
                    "tags": [*tags, "synthesis"],
                }
            )
    else:
        rebuilt_lines = []
        if synthesis and synthesis.get("insights"):
            rebuilt_lines.extend(f"- {insight['summary']}" for insight in synthesis["insights"][:5])
        elif reflection and reflection.get("insights"):
            rebuilt_lines.extend(f"- {insight['summary']}" for insight in reflection["insights"][:5])
        else:
            rebuilt_lines.extend(f"- {row['content']}" for row in source_rows[:5])
        entries.append(
            {
                "content": "Rebuilt memory bank:\n" + "\n".join(rebuilt_lines),
                "category": "curation_rebuild",
                "scope": "arc",
                "retention": "long_term",
                "tags": [*tags, "rebuilt"],
            }
        )

    return entries


def _insert_curation_entries(
    uid: str,
    tenant_id: str,
    bank_id: str,
    entries: list[dict[str, Any]],
    *,
    cancel_event: threading.Event | None = None,
) -> list[str]:
    """Persist curated output entries into the chosen bank."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db_connection()
    created_ids: list[str] = []
    try:
        conn.execute("BEGIN")
        for entry in entries:
            if cancel_event and cancel_event.is_set():
                raise CurationError("Curation canceled before staged output committed")
            memory_id = hashlib.sha256(f"{bank_id}:{entry['content']}:{uuid.uuid4().hex}".encode()).hexdigest()[:16]
            conn.execute(
                "INSERT INTO memories "
                "(id, content, scope, retention, category, user_id, bank_id, tenant_id, "
                "created_at, updated_at, tags, emotional_context, metrics, is_ghost, synthesized_from, importance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}', 0, '[]', ?)",
                (
                    memory_id,
                    entry["content"],
                    entry.get("scope", "arc"),
                    entry.get("retention", "long_term"),
                    entry.get("category", "curation"),
                    uid,
                    bank_id,
                    tenant_id,
                    now,
                    now,
                    json.dumps(entry.get("tags", [])),
                    entry.get("importance", 0.75),
                ),
            )
            created_ids.append(memory_id)
        if cancel_event and cancel_event.is_set():
            raise CurationError("Curation canceled before staged output committed")
        conn.commit()
        return created_ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _execute_curation_run(run_id: str, payload: dict[str, Any]) -> None:
    """Execute a queued curation run."""
    tenant_id = payload["tenant_id"]
    uid = payload["user_id"]
    cancel_event = _get_curation_cancel_event(run_id)
    set_current_tenant_id(tenant_id)
    queue = OperationQueue(DB_PATH)
    now = datetime.now(timezone.utc).isoformat()
    if not _claim_curation_run(run_id, tenant_id, now):
        return
    _publish_curation_status(run_id, "running", actor=uid, source_bank_id=payload["source_bank_id"])

    try:
        _raise_if_run_canceled(uid, tenant_id, run_id)
        if payload.get("transcript_bundle"):
            agent = get_context_block_agent(uid, tenant_id)
            session_id = payload.get("session_id") or f"curation-{run_id}"
            _run_async(
                agent.process_transcript(
                    session_id=session_id,
                    messages=payload["transcript_bundle"],
                    project_path=payload.get("project_path"),
                )
            )
            _bridge_context_blocks_to_memories(agent, uid)
            _bridge_transcript_entities(payload["transcript_bundle"], uid)

        _raise_if_run_canceled(uid, tenant_id, run_id)
        source_rows = _load_source_bank_rows(uid, tenant_id, payload["source_bank_id"])
        block_snapshot = _context_block_snapshot(uid, tenant_id)
        synthesis = None if payload["tool_access"] == "disabled" else _build_synthesis_snapshot(source_rows, uid)
        reflection = (
            None if payload["tool_access"] == "disabled" else _build_reflection_snapshot(source_rows, uid, tenant_id)
        )
        run = {
            "id": run_id,
            "source_bank_id": payload["source_bank_id"],
            "output_bank_id": payload["output_bank_id"],
            "policy_mode": payload["policy_mode"],
            "tool_access": payload["tool_access"],
            "output_mode": payload["output_mode"],
            "instructions": payload.get("instructions"),
        }
        entries = _make_curated_entries(run, source_rows, block_snapshot, synthesis, reflection)
        _delete_existing_curation_outputs(uid, tenant_id, payload["output_bank_id"], run_id)
        created_ids = _insert_curation_entries(
            uid,
            tenant_id,
            payload["output_bank_id"],
            entries,
            cancel_event=cancel_event,
        )
        _raise_if_run_canceled(uid, tenant_id, run_id)

        promotion_summary: dict[str, Any] = {}
        if payload["output_mode"] == "in_place":
            promotion_summary = _promote_in_place_curation(
                uid,
                tenant_id,
                run_id,
                payload["source_bank_id"],
                payload["output_bank_id"],
                source_rows,
                created_ids,
                cancel_event=cancel_event,
            )
            if cancel_event.is_set() or _is_run_canceled(uid, tenant_id, run_id):
                _restore_in_place_curation(
                    uid,
                    tenant_id,
                    payload["source_bank_id"],
                    payload["output_bank_id"],
                    source_rows,
                    created_ids,
                )
                raise CurationError("Curation canceled after promotion; restored source bank")

        # Run clustering after curation if requested
        clustering_summary: dict[str, Any] | None = None
        if payload.get("run_clustering"):
            try:
                _raise_if_run_canceled(uid, tenant_id, run_id)
                cluster_memories_for_run = _fetch_memories_for_clustering(uid, tenant_id)
                if cluster_memories_for_run:
                    cluster_result = cluster_memories(cluster_memories_for_run)
                    if cluster_result.cluster_entities:
                        clustering_summary = _upsert_cluster_results(cluster_result, uid, tenant_id)
            except Exception as exc:
                logger.warning("Clustering post-curation failed for run %s: %s", run_id, exc)
                clustering_summary = {"error": str(exc)}

        summary = {
            "source_memory_count": len(source_rows),
            "output_memory_count": len(created_ids),
            "output_memory_ids": created_ids,
            "context_blocks_considered": [block["label"] for block in block_snapshot],
            "synthesis": synthesis,
            "reflection": reflection,
            "transcript_processed": bool(payload.get("transcript_bundle")),
            "clustering": clustering_summary,
        }
        summary.update(promotion_summary)
        _raise_if_run_canceled(uid, tenant_id, run_id)
        completed_at = datetime.now(timezone.utc).isoformat()
        _update_curation_run(run_id, tenant_id, status="completed", summary=summary, ended_at=completed_at, error={})
        queue.remove(run_id, tenant_id=tenant_id)
        _publish_curation_status(
            run_id,
            "completed",
            actor=uid,
            output_bank_id=payload["output_bank_id"],
            source_bank_id=payload["source_bank_id"],
            output_mode=payload["output_mode"],
        )
    except CurationError:
        ended_at = datetime.now(timezone.utc).isoformat()
        row = _fetch_curation_run(uid, tenant_id, run_id)
        if row is None or row["status"] != "canceled":
            _update_curation_run(run_id, tenant_id, status="canceled", ended_at=ended_at)
            _publish_curation_status(run_id, "canceled", actor=uid)
        else:
            _update_curation_run(run_id, tenant_id, ended_at=ended_at)
        queue.remove(run_id, tenant_id=tenant_id)
    except Exception as exc:
        failed_at = datetime.now(timezone.utc).isoformat()
        error = {
            "type": exc.__class__.__name__,
            "message": str(exc),
        }
        _update_curation_run(run_id, tenant_id, status="failed", error=error, ended_at=failed_at)
        queue.remove(run_id, tenant_id=tenant_id)
        _publish_curation_status(run_id, "failed", actor=uid, error=error)
    finally:
        with _CURATION_WORKERS_LOCK:
            _CURATION_WORKERS.pop(run_id, None)
            _CURATION_CANCEL_SIGNALS.pop(run_id, None)


def _start_curation_worker(run_id: str, payload: dict[str, Any]) -> None:
    """Start a daemon thread to process a curation run."""
    worker = threading.Thread(
        target=_execute_curation_run,
        args=(run_id, payload),
        daemon=True,
        name=f"foresight-curation-{run_id[:8]}",
    )
    with _CURATION_WORKERS_LOCK:
        existing = _CURATION_WORKERS.get(run_id)
        if existing and existing.is_alive():
            return
        _CURATION_WORKERS[run_id] = worker
    worker.start()


@mcp.tool(output_schema=None)
def manage_curation_runs(
    options: CurationRunAction | None = None,
    user_id: str | None = None,
    action: Literal["create", "get", "list", "cancel", "archive"] | None = None,
    run_id: str | None = None,
    source_bank_id: str | None = None,
    output_bank_id: str | None = None,
    policy_mode: Literal["preserve", "rebalance", "rebuild"] | None = None,
    tool_access: Literal["disabled", "observe", "operate"] | None = None,
    output_mode: Literal["reviewable_output", "in_place"] | None = None,
    instructions: str | None = None,
    run_clustering: bool | None = None,
    transcript_bundle: list[dict] | None = None,
    session_id: str | None = None,
    project_path: str | None = None,
) -> str:
    """
    Manage async Foresight curation runs.

    Args:
        options: CurationRunAction options payload
        user_id: Optional user ID override
        action: Flat parameter for action (optional, fallback if options not provided)
        run_id: Flat parameter for run ID (optional)
        source_bank_id: Flat parameter for source bank ID (optional)
        output_bank_id: Flat parameter for output bank ID (optional)
        policy_mode: Flat parameter for policy mode (optional)
        tool_access: Flat parameter for tool access (optional)
        output_mode: Flat parameter for output mode (optional)
        instructions: Flat parameter for instructions (optional)
        run_clustering: Flat parameter for run clustering (optional)
        transcript_bundle: Flat parameter for transcript bundle (optional)
        session_id: Flat parameter for session ID (optional)
        project_path: Flat parameter for project path (optional)
    """
    if options is None:
        if action is None:
            return "Error: either 'options' or 'action' must be provided."
        options = CurationRunAction(
            action=action,
            run_id=run_id,
            source_bank_id=source_bank_id,
            output_bank_id=output_bank_id,
            policy_mode=policy_mode if policy_mode is not None else "rebalance",
            tool_access=tool_access if tool_access is not None else "observe",
            output_mode=output_mode if output_mode is not None else "reviewable_output",
            instructions=instructions,
            run_clustering=run_clustering if run_clustering is not None else False,
            transcript_bundle=transcript_bundle,
            session_id=session_id,
            project_path=project_path,
        )

    init_db()
    uid = user_id or USER_ID
    tenant_id = get_current_account_id()

    if options.action == "create":
        source_bank_id = options.source_bank_id or BANK_ID
        if options.output_mode == "in_place" and options.tool_access != "operate":
            res = _tool_error("create", "output_mode=in_place requires tool_access=operate")
        elif options.output_mode == "in_place" and options.output_bank_id is not None:
            res = _tool_error("create", "output_mode=in_place does not allow output_bank_id override")
        elif options.transcript_bundle and options.tool_access != "operate":
            res = _tool_error("create", "transcript_bundle requires tool_access=operate")
        else:
            run_id = f"cur_{uuid.uuid4().hex[:12]}"
            output_bank_id = _curation_run_output_bank(
                run_id, source_bank_id, options.output_mode, options.output_bank_id
            )
            created_at = datetime.now(timezone.utc).isoformat()
            conn = get_db_connection()
            conn.execute(
                """INSERT INTO curation_runs
                (id, tenant_id, user_id, source_bank_id, output_bank_id, policy_mode, tool_access,
                 output_mode, status, instructions, transcript_bundle_json, session_id, project_path,
                 summary_json, error_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, '{}', '{}', ?)""",
                (
                    run_id,
                    tenant_id,
                    uid,
                    source_bank_id,
                    output_bank_id,
                    options.policy_mode,
                    options.tool_access,
                    options.output_mode,
                    options.instructions,
                    json.dumps(options.transcript_bundle) if options.transcript_bundle else None,
                    options.session_id,
                    options.project_path,
                    created_at,
                ),
            )
            conn.commit()
            conn.close()

            payload = {
                "tenant_id": tenant_id,
                "user_id": uid,
                "source_bank_id": source_bank_id,
                "output_bank_id": output_bank_id,
                "policy_mode": options.policy_mode,
                "tool_access": options.tool_access,
                "output_mode": options.output_mode,
                "instructions": options.instructions,
                "run_clustering": options.run_clustering,
                "transcript_bundle": options.transcript_bundle,
                "session_id": options.session_id,
                "project_path": options.project_path,
            }
            queue = OperationQueue(DB_PATH)
            queue.enqueue(
                Operation(
                    id=run_id,
                    type=OperationType.CREATE,
                    entity_type="curation_run",
                    entity_id=run_id,
                    payload=payload,
                ),
                tenant_id=tenant_id,
            )
            _publish_curation_status(run_id, "pending", actor=uid, output_bank_id=output_bank_id)
            _start_curation_worker(run_id, payload)
            run = _row_to_curation_run(_fetch_curation_run(uid, tenant_id, run_id))
            res = _tool_response(ok=True, action="create", run=run)

    elif options.action == "list":
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT * FROM curation_runs WHERE user_id = ? AND tenant_id = ? ORDER BY created_at DESC LIMIT ?",
            (uid, tenant_id, options.limit),
        ).fetchall()
        conn.close()
        res = _tool_response(ok=True, action="list", runs=[_row_to_curation_run(row) for row in rows])

    elif not options.run_id:
        res = _tool_error(options.action, "run_id is required for this action")

    else:
        row = _fetch_curation_run(uid, tenant_id, options.run_id)
        if row is None:
            res = _tool_error(options.action, f"Curation run {options.run_id} not found.", run_id=options.run_id)
        elif options.action == "get":
            res = _tool_response(ok=True, action="get", run=_row_to_curation_run(row))
        elif options.action == "cancel":
            if row["status"] not in {"pending", "running"}:
                res = _tool_error(
                    "cancel",
                    f"Run {options.run_id} is already {row['status']} and cannot be canceled.",
                    run=_row_to_curation_run(row),
                )
            else:
                ended_at = datetime.now(timezone.utc).isoformat()
                _get_curation_cancel_event(options.run_id).set()
                _update_curation_run(options.run_id, tenant_id, status="canceled", ended_at=ended_at)
                OperationQueue(DB_PATH).remove(options.run_id, tenant_id=tenant_id)
                _publish_curation_status(options.run_id, "canceled", actor=uid)
                res = _tool_response(
                    ok=True,
                    action="cancel",
                    run=_row_to_curation_run(_fetch_curation_run(uid, tenant_id, options.run_id)),
                )
        elif options.action == "archive":
            if row["status"] not in {"completed", "failed", "canceled"}:
                res = _tool_error(
                    "archive",
                    f"Run {options.run_id} must be terminal before it can be archived.",
                    run=_row_to_curation_run(row),
                )
            else:
                archived_at = datetime.now(timezone.utc).isoformat()
                _update_curation_run(options.run_id, tenant_id, archived_at=archived_at)
                res = _tool_response(
                    ok=True,
                    action="archive",
                    run=_row_to_curation_run(_fetch_curation_run(uid, tenant_id, options.run_id)),
                )
        else:
            res = _tool_error(options.action, f"Unsupported action: {options.action}")

    return res


# =============================================================================
# In-Context Memory Injection
# =============================================================================

_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "this",
        "that",
        "these",
        "those",
        "i",
        "you",
        "he",
        "she",
        "it",
        "we",
        "they",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "his",
        "its",
        "our",
        "their",
        "and",
        "but",
        "or",
        "not",
        "no",
        "so",
        "if",
        "then",
        "than",
        "too",
        "very",
        "just",
        "about",
        "also",
        "with",
        "from",
        "into",
        "for",
        "on",
        "at",
        "to",
        "of",
        "in",
        "by",
        "up",
        "out",
        "off",
        "all",
        "some",
        "any",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "such",
        "only",
        "own",
        "same",
        "what",
        "when",
        "where",
        "who",
        "how",
        "why",
        "which",
        "while",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "again",
        "further",
        "once",
    }
)


def _extract_terms(text: str) -> list[str]:
    """Extract key terms from text by splitting, lowering, and filtering stop words and short tokens."""
    words = text.lower().split()
    return [w for w in words if len(w) > 3 and w not in _STOP_WORDS]


class _MemoryLike(Protocol):
    """Minimal row protocol for relevance scoring."""

    def __getitem__(self, key: str, /) -> Any: ...


def _score_memory_relevance(
    memory: _MemoryLike | Mapping[str, Any],
    terms: list[str],
    now: datetime,
) -> float:
    """Compute a relevance score for a memory row given search terms.

    Score = term-overlap-count + importance-boost + recency-decay

    - term-overlap-count: how many of the search terms appear in the memory content
    - importance-boost: the stored importance value (default 1.0)
    - recency-decay: exponential decay based on age in days (half-life ~7 days)
    """
    content_lower = (memory["content"] or "").lower()

    overlap = sum(1 for t in terms if re.search(rf"\b{re.escape(t)}\b", content_lower))

    overlap_score = overlap / max(len(terms), 1)

    importance = memory["importance"] if memory["importance"] is not None else 0.5

    created_str = memory["created_at"]
    try:
        created = datetime.fromisoformat(created_str)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = max((now - created).total_seconds() / 3600, 0)
    except (ValueError, TypeError):
        age_hours = 0
    half_life_hours = 168.0
    decay = 0.5 ** (age_hours / half_life_hours)

    return overlap_score + importance * 0.5 + decay * 0.5


@mcp.tool(output_schema=None)
def inject_context(
    conversation_text: str,
    user_id: str | None = None,
    max_memories: int = 5,
    min_relevance: float = 0.01,
    include_details: bool = False,
    max_chars: int | None = None,
) -> str:
    """Surface relevant memories and context blocks for the current conversation.

    ## When to call this (automatic / proactive use)

    Call this tool **without waiting to be asked** in these situations:

    - **At conversation start** — as soon as the user's first message arrives,
      pass it as `conversation_text` so prior context and guidance are available
      before you compose your first reply.
    - **On topic shift** — whenever the user introduces a new subject, project,
      person, or task that differs from the previous topic, call again with the
      new message so relevant memories surface immediately.
    - **On explicit recall signals** — phrases like "last time", "as we discussed",
      "remember when", "you mentioned", or any reference to past work are strong
      signals to call this right away.
    - **Before acting on a task** — before writing code, making a plan, or giving
      advice on a non-trivial request, call first so your answer is grounded in
      what the user has already told you.

    The output is short and additive — it will not hurt responses when nothing
    relevant exists; it will only improve them when something does.

    ## Parameters

    Args:
        conversation_text: The current conversation text to analyze for context
        user_id: Optional user ID override
        max_memories: Maximum number of memories to return (default: 5)
        min_relevance: Minimum relevance score threshold (default: 0.01, RRF scores are typically 0.01-0.08)
        include_details: If True, return JSON with formatted text plus structured
            memories and context blocks grouped by InjectionPoint (default: False)
        max_chars: Optional character budget for the formatted payload.
            When set, output is truncated at sentence boundaries per lane
            priority (static > dynamic > memories > blocks > safety).
            Items that don't fit are progressively summarized or stubbed.
            Default None = unbounded (legacy behavior).

    Returns:
        Formatted string with relevant memories and context block signals.
        If include_details=True, returns a JSON string with keys:
        - formatted: the formatted text
        - memories: list of dicts with memory_id, content, score, signals
        - context_blocks: dict grouped by InjectionPoint
        - budget: lane allocation details (only when max_chars is set)
    """
    uid = user_id or USER_ID
    tenant_id = get_current_account_id()
    terms = _extract_terms(conversation_text)
    retriever = get_hybrid_retriever()
    query_text = conversation_text if conversation_text else " ".join(terms)

    t0 = time.perf_counter()
    hybrid_result = retriever.search(
        query=query_text,
        user_id=uid,
        options=HybridSearchOptions(
            tenant_id=tenant_id,
            limit=max(50, max_memories * 3),
            min_importance=0.1,
            use_keyword=True,
            use_tfidf_cosine=True,
            use_semantic=None,
            use_graph=True,
            use_temporal=True,
        ),
    )
    latency_ms = (time.perf_counter() - t0) * 1000
    memories: list[HybridResult] = [r for r in hybrid_result.results if r.combined_score >= min_relevance][
        :max_memories
    ]
    # Track last injection for system status visibility (PIX-3955)
    _last_injection_stats.update(
        {
            "last_run_at": datetime.now(timezone.utc).isoformat(),
            "query_length": len(conversation_text),
            "latency_ms": round(latency_ms, 2),
            "memories_fetched": len(hybrid_result.results),
            "memories_returned": len(memories),
            "fast_path": hybrid_result.signal_counts.get("fast_path"),
            "signal_counts": dict(hybrid_result.signal_counts),
            "max_memories_requested": max_memories,
            "min_relevance": min_relevance,
            "max_chars": max_chars,
        }
    )
    logger.debug(
        "inject_context: query_len=%d latency_ms=%.2f fetched=%d filtered=%d fast_path=%s signals=%s",
        len(conversation_text),
        latency_ms,
        len(hybrid_result.results),
        len(memories),
        hybrid_result.signal_counts.get("fast_path"),
        hybrid_result.signal_counts,
    )

    # Reinforce surfaced memories so retrieval strengthens them (closes the
    # decay loop: decay weakens, retrieval strengthens).
    if memories:
        _auto_reinforce_batch([m.memory_id for m in memories], uid, tenant_id)

    # Side-effect: silently capture any phrase-triggered memories in the message
    # ("remember this:", "decision:", "preference:", etc.) without the user
    # needing to call a separate tool.
    if conversation_text:
        try:
            _auto_capture_triggered(conversation_text, uid, tenant_id)
        except Exception:
            logger.debug("auto_capture_triggered failed (non-fatal)", exc_info=True)

    budget = InjectionBudget(max_chars=max_chars) if max_chars is not None else None

    if budget is not None and budget.is_bounded:
        budgeted = _format_injection_output_budgeted(memories, uid, tenant_id, terms, budget)
    else:
        budgeted = None

    if not include_details:
        return budgeted.formatted if budgeted is not None else _format_injection_output(memories, uid, tenant_id, terms)

    blocks_by_point = _format_context_blocks_by_injection_point(uid, tenant_id, terms)
    legacy_formatted = _format_injection_output(memories, uid, tenant_id, terms)
    payload: dict[str, Any] = {
        "formatted": budgeted.formatted if budgeted is not None else legacy_formatted,
        "memories": [m.to_dict() for m in memories],
        "context_blocks": blocks_by_point,
    }
    if budgeted is not None:
        payload["budget"] = budgeted.to_dict()
    return json.dumps(payload, indent=2)


def _auto_capture_triggered(text: str, uid: str, tenant_id: str) -> None:
    """Fire-and-forget phrase trigger capture called as a side effect of inject_context.

    Scans the conversation text for trigger phrases ("remember this:", "decision:",
    "preference:", "lesson:", etc.) and automatically stores matching content as
    memories. Users get persistent memory just by typing naturally — no separate
    tool call required.
    """
    matches = extract_triggered_memories(text, triggers=DEFAULT_TRIGGERS)
    for match in matches:
        try:
            _handle_memory_store(
                uid,
                tenant_id,
                MemoryAction(
                    action="store",
                    content=match.content,
                    options=MemoryOptions(
                        category=match.metadata.get("category", "fact"),
                        scope=match.metadata.get("scope", "arc"),
                        retention=match.metadata.get("retention", "long_term"),
                        importance=float(match.metadata.get("importance", 0.6)),
                    ),
                ),
            )
            logger.debug("auto_capture_triggered: stored %r (trigger=%r)", match.content[:60], match.trigger)
        except Exception:
            logger.debug("auto_capture_triggered store failed (non-fatal)", exc_info=True)


def _format_injection_output(
    memories: list[HybridResult],
    uid: str,
    tenant_id: str,
    terms: list[str],
) -> str:
    """Format memories and context block signals into a human-readable string.

    Always surfaces all non-empty context blocks (not just term-matched ones) so
    user_preferences and pending_items are visible regardless of query keywords.
    Returns an empty string when there is nothing relevant — no noise for the LLM.
    """
    lines: list[str] = []
    if memories:
        lines.append(f"[Relevant Context - {len(memories)} memories surfaced]")
        for mem in memories:
            snippet = (mem.content or "")[:120]
            if len(mem.content or "") > 120:
                snippet += "..."
            lines.append(f"- [{mem.memory_id}] (score: {mem.combined_score:.2f}) {snippet}")

    # Always surface ALL non-empty context blocks, not just term-matched ones.
    # A preference set days ago is still relevant even if its words aren't in
    # today's query.
    try:
        all_blocks = get_context_block_agent(uid, tenant_id).get_all_blocks()
        if all_blocks:
            if lines:
                lines.append("")
            lines.append("[Context Blocks]")
            for block in all_blocks:
                label = block.get("label", "")
                content = (block.get("content") or "").strip()
                if content:
                    snippet = content[:200] + ("..." if len(content) > 200 else "")
                    lines.append(f"  [{label}] {snippet}")
    except Exception:
        # Fall back to term-matched block signals if full block fetch fails
        sub_lines = _context_block_notes_for_terms(uid, terms)
        if sub_lines:
            if lines:
                lines.append("")
            lines.append("[Context Blocks]")
            lines.extend(sub_lines)

    # Return empty string when nothing is relevant — cleaner than a noisy
    # "0 memories surfaced" message that clutters the LLM's context.
    return "\n".join(lines) if lines else ""


def _format_injection_output_budgeted(
    memories: list[HybridResult],
    uid: str,
    tenant_id: str,
    terms: list[str],
    budget: InjectionBudget,
) -> BudgetResult:
    lane_items: dict[Lane, list[LaneItem]] = {lane: [] for lane in Lane}

    user_prefs = get_context_block_agent(uid, tenant_id).state.get_block(USER_PREFERENCES)
    if user_prefs and not user_prefs.is_empty():
        lane_items[Lane.STATIC].append(
            LaneItem(id="user_preferences", content=user_prefs.content, score=0.9, lane=Lane.STATIC)
        )

    project_ctx = get_context_block_agent(uid, tenant_id).state.get_block("project_context")
    pending = get_context_block_agent(uid, tenant_id).state.get_block(PENDING_ITEMS)
    if project_ctx and not project_ctx.is_empty():
        lane_items[Lane.DYNAMIC].append(
            LaneItem(id="project_context", content=project_ctx.content, score=0.8, lane=Lane.DYNAMIC)
        )
    if pending and not pending.is_empty():
        lane_items[Lane.DYNAMIC].append(
            LaneItem(id="pending_items", content=pending.content, score=0.7, lane=Lane.DYNAMIC)
        )

    for mem in memories:
        lane_items[Lane.MEMORIES].append(
            LaneItem(
                id=mem.memory_id,
                content=mem.content or "",
                score=mem.combined_score,
                lane=Lane.MEMORIES,
                metadata={"category": mem.category, "importance": mem.importance},
            )
        )

    sub_lines = _context_block_notes_for_terms(uid, terms)
    if sub_lines:
        lane_items[Lane.BLOCKS].append(
            LaneItem(id="block_signals", content="\n".join(sub_lines), score=0.5, lane=Lane.BLOCKS)
        )

    return format_budgeted_payload(lane_items, budget, header="[Relevant Context]")


def _format_context_blocks_by_injection_point(
    uid: str,
    tenant_id: str,
    terms: list[str],
) -> dict[str, list[dict]]:
    """Group matching context block entries by their schema's InjectionPoint.

    Returns a dict mapping InjectionPoint value to matching block entries:
    {"pre_prompt": [...], "post_prompt": [...], "whisper_only": [...]}

    Each entry contains: label, content, matched_terms.
    """
    agent = get_context_block_agent(uid, tenant_id)
    registry = initialize_default_blocks()
    relevant_labels = [USER_PREFERENCES, SESSION_PATTERNS, PENDING_ITEMS]

    grouped: dict[str, list[dict]] = {
        InjectionPoint.PRE_PROMPT.value: [],
        InjectionPoint.POST_PROMPT.value: [],
        InjectionPoint.WHISPER_ONLY.value: [],
    }

    for label in relevant_labels:
        schema = registry.get_schema(label)
        if schema is None:
            continue
        block = agent.state.get_block(label)
        if not block or block.is_empty():
            continue
        content = block.content
        content_lower = content.lower()
        if not terms or not any(re.search(rf"\b{re.escape(t)}\b", content_lower) for t in terms):
            continue
        for line in content.splitlines():
            line_lower = line.lower().strip()
            if not line_lower:
                continue
            matched = [t for t in terms if re.search(rf"\b{re.escape(t)}\b", line_lower)]
            if matched:
                grouped[schema.injection_point.value].append(
                    {"label": label, "content": line.strip(), "matched_terms": matched}
                )

    return grouped


def _context_block_notes_for_terms(
    uid: str,
    terms: list[str],
) -> list[str]:
    """Check context blocks for content relevant to the search terms.

    Returns a list of formatted lines with matching block content,
    grouped by InjectionPoint (PRE_PROMPT first, then POST_PROMPT, then WHISPER_ONLY).
    """
    tenant_id = get_current_account_id()
    grouped = _format_context_blocks_by_injection_point(uid, tenant_id, terms)

    lines: list[str] = []
    for point_value, entries in grouped.items():
        matching_entries = [
            e for e in entries if any(re.search(rf"\b{re.escape(t)}\b", e["content"].lower()) for t in terms)
        ]
        if not matching_entries:
            continue
        for entry in matching_entries[:3]:
            lines.append(f"  [{entry['label']} / {point_value}] {entry['content']}")

    return lines


def get_relevant_memories(
    query: str,
    user_id: str | None = None,
    limit: int = 5,
    min_relevance: float = 0.01,
    max_chars: int | None = None,
) -> str:
    """Return structured list of relevant memories for a query.

    Clean memories-only API (no context blocks). Uses HybridRetriever
    (keyword + TF-IDF + graph + temporal signals via RRF) for ranking.

    Args:
        query: Search query string
        user_id: Optional user ID override
        limit: Maximum number of memories to return (default: 5)
        min_relevance: Minimum combined_score threshold (default: 0.01, RRF scores are typically 0.01-0.08)
        max_chars: Optional character budget for memory content. When set,
            each memory's content is truncated at sentence boundaries
            if the total payload would exceed this limit. Default None
            = unbounded (legacy behavior).

    Returns:
        JSON string with:
        - memories: list of dicts with memory_id, content, category, importance,
          created_at, keyword_score, tfidf_cosine_score, semantic_score,
          graph_score, temporal_score, combined_score, source_signals
        - total_candidates: number of candidates considered
        - signal_counts: dict of how many results each signal contributed
        - budget: lane allocation details (only when max_chars is set)
    """
    uid = user_id or USER_ID
    tenant_id = get_current_account_id()
    retriever = get_hybrid_retriever()

    t0 = time.perf_counter()
    result = retriever.search(
        query=query,
        user_id=uid,
        options=HybridSearchOptions(
            tenant_id=tenant_id,
            limit=max(limit * 2, 20),
            min_importance=0.0,
        ),
    )
    latency_ms = (time.perf_counter() - t0) * 1000
    filtered_results = [m for m in result.results if m.combined_score >= min_relevance][:limit]
    memories = [m.to_dict() for m in filtered_results]

    payload: dict[str, Any] = {
        "memories": memories,
        "total_candidates": result.total_candidates,
        "signal_counts": result.signal_counts,
        "_trace": {
            "latency_ms": round(latency_ms, 2),
            "fast_path": result.signal_counts.get("fast_path"),
        },
    }

    if max_chars is not None:
        budget = InjectionBudget(max_chars=max_chars)
        lane_items = {
            Lane.MEMORIES: [
                LaneItem(id=m.memory_id, content=m.content or "", score=m.combined_score, lane=Lane.MEMORIES)
                for m in filtered_results
            ]
        }
        budgeted = format_budgeted_payload(lane_items, budget, header="[Relevant Memories]")
        payload["budget"] = budgeted.to_dict()
        mem_alloc = budgeted.allocations.get(Lane.MEMORIES)
        if mem_alloc is not None:
            for item, _ in mem_alloc.summary_items:
                for mem_dict in payload["memories"]:
                    if mem_dict.get("memory_id") == item.id:
                        mem_dict["_truncation"] = "summary"
            for item, _ in mem_alloc.stub_items:
                for mem_dict in payload["memories"]:
                    if mem_dict.get("memory_id") == item.id:
                        mem_dict["_truncation"] = "stub"

    logger.debug(
        "get_relevant_memories: query=%r latency_ms=%.2f candidates=%d returned=%d fast_path=%s signals=%s budget=%s",
        query,
        latency_ms,
        result.total_candidates,
        len(memories),
        result.signal_counts.get("fast_path"),
        result.signal_counts,
        max_chars,
    )
    return json.dumps(payload, indent=2)


# =============================================================================
# Recovery Payload for Session Resume / Compaction
# =============================================================================


def _fetch_session_memories_raw(
    uid: str,
    tenant_id: str,
    limit: int = 20,
) -> list[RowLike]:
    """Fetch session-scoped memories for a user, ordered by importance desc."""
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT id, content, importance, scope, category, created_at, tags "
            "FROM memories "
            "WHERE user_id = ? AND tenant_id = ? AND scope = 'session' AND is_ghost = 0 "
            "ORDER BY importance DESC, created_at DESC "
            "LIMIT ?",
            (uid, tenant_id, limit),
        ).fetchall()
    finally:
        conn.close()


def _fetch_high_confidence_memories(
    uid: str,
    tenant_id: str,
    min_importance: float = 0.5,
    limit: int = 20,
) -> list[RowLike]:
    """Fetch high-confidence project-level memories (arc/fact/trait) as fallback."""
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT id, content, importance, scope, category, created_at, tags "
            "FROM memories "
            "WHERE user_id = ? AND tenant_id = ? "
            "AND scope IN ('arc', 'fact', 'trait') "
            "AND importance >= ? AND is_ghost = 0 "
            "ORDER BY importance DESC, created_at DESC "
            "LIMIT ?",
            (uid, tenant_id, min_importance, limit),
        ).fetchall()
    finally:
        conn.close()


def generate_recovery_payload(
    session_id: str,
    user_id: str | None = None,
    max_chars: int | None = None,
    exclude_memory_ids: str | None = None,
) -> str:
    """Generate a compact recovery payload for session resume or context compaction.

    Creates a budgeted, deduplicated memory block containing only the most
    relevant session and project memories. Designed for injection after
    session compaction or agent resume events where full transcript history
    is no longer available.

    Uses the same lane-based budget system (PIX-3949) as inject_context.

    Memory sourcing (two-phase):
      1. Session-scoped memories (scope='session') — most relevant to the
         session being recovered.
      2. High-confidence project memories (arc/fact/trait with importance ≥ 0.5)
         — fallback when few session memories exist.

    Args:
        session_id: Unique session identifier (used for diagnostics and
            future session-scoped querying).
        user_id: Optional user ID override.
        max_chars: Optional character budget for the formatted payload.
            When set, output is truncated at sentence boundaries per lane
            priority (session > project > blocks).
            Default None = unbounded (legacy behavior).
        exclude_memory_ids: Optional comma-separated list of memory IDs
            to exclude (e.g. memories already present in current context).
            Enables dedup after compaction.

    Returns:
        Formatted string with recovery payload.
        Returns minimal message when no relevant memories exist.
    """
    uid = user_id or get_current_user_id()
    tenant_id = get_current_account_id()

    excluded: set[str] = set()
    if exclude_memory_ids:
        excluded = {mid.strip() for mid in exclude_memory_ids.split(",") if mid.strip()}

    session_rows = _fetch_session_memories_raw(uid, tenant_id, limit=20)

    project_rows: list[RowLike] = []
    if len(session_rows) < 10:
        project_rows = _fetch_high_confidence_memories(uid, tenant_id, min_importance=0.5, limit=20)

    seen_contents: set[str] = set()

    def _dedup_rows(rows: list[RowLike], session_boost: float) -> list[LaneItem]:
        items: list[LaneItem] = []
        for row in rows:
            row_id = row["id"]
            row_content = row["content"] or ""
            if row_id in excluded:
                continue
            content_digest = hashlib.sha256(row_content.encode()).hexdigest()[:16]
            if content_digest in seen_contents:
                continue
            seen_contents.add(content_digest)
            importance = row["importance"] if row["importance"] is not None else 0.5
            score = min(importance + session_boost, 1.0)
            items.append(
                LaneItem(
                    id=row_id,
                    content=row_content,
                    score=score,
                    lane=Lane.MEMORIES,
                    metadata={"scope": row["scope"], "importance": importance},
                )
            )
        return items

    session_items = _dedup_rows(session_rows, session_boost=0.2)
    project_items = _dedup_rows(project_rows, session_boost=0.0)

    budget = InjectionBudget(max_chars=max_chars) if max_chars is not None else InjectionBudget()
    lane_items: dict[Lane, list[LaneItem]] = {Lane.MEMORIES: session_items + project_items}

    total_items = len(session_items) + len(project_items)

    if total_items == 0:
        return "[Recovery Context - 0 memories]\nNo session or project memories available for recovery."

    result = format_budgeted_payload(lane_items, budget, header=f"[Recovery Context - session: {session_id}]")

    return result.formatted


@mcp.tool(output_schema=None)
def compaction_lifecycle(
    session_id: str,
    messages: list[dict],
    exclude_memory_ids: str | None = None,
    max_chars: int | None = None,
    project_path: str | None = None,
) -> str:
    """Capture memories from about-to-be-lost transcript, then return recovery payload.

    Designed for context-compaction lifecycle: call BEFORE the context window
    is compressed so Foresight extracts and stores decisions/lessons from the
    transcript, then inject the returned recovery payload AFTER compaction
    to restore essential context without the agent needing to cooperate.

    Combines process_session_transcript (capture) and generate_recovery_payload
    (recover) into a single MCP call. The capture step is best-effort: if it
    fails, the recovery payload is still returned so the caller always gets
    context to inject. This is NOT atomic — there is no rollback if recovery
    fails after capture succeeds.

    PHI sensitivity: ``messages`` may contain clinical or patient-identifiable
    content. All captured memories are stored under the authenticated user's
    scope (``get_current_user_id()``) and tenant (``get_current_account_id()``).
    Callers must ensure transcript content is appropriate to persist before
    calling this tool.

    Args:
        session_id: Unique session identifier.
        messages: List of message dicts with role/content — the transcript
            about to be lost to compaction.
        exclude_memory_ids: Optional comma-separated memory IDs already in
            context (prevents re-injecting duplicates after compaction).
        max_chars: Optional character budget for the recovery payload.
            Truncates at sentence boundaries by lane priority
            (session > project > blocks).
        project_path: Optional project path for extraction context.
            Auto-detected from git root when omitted.

    Returns:
        Recovery payload string for immediate injection into the
        freshly compacted context window. The payload respects ``max_chars``
        — capture status is logged but never prepended to the result.
    """
    uid = get_current_user_id()

    # 1. Capture: extract and store memories from the about-to-be-lost transcript.
    #    Best-effort — if capture fails we still return the recovery payload.
    try:
        process_session_transcript(
            session_id=session_id,
            messages=messages,
            project_path=project_path,
            user_id=uid,
        )
    except Exception:
        logger.warning(
            "compaction_lifecycle: capture failed for session %s; returning recovery payload without new memories",
            session_id,
            exc_info=True,
        )

    # 2. Recover: generate compact payload for re-injection
    recovery_payload = generate_recovery_payload(
        session_id=session_id,
        user_id=uid,
        exclude_memory_ids=exclude_memory_ids,
        max_chars=max_chars,
    )

    return recovery_payload


def _resume_pending_curation_runs() -> None:
    """Resume pending or interrupted curation runs on server startup."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM curation_runs WHERE status IN ('pending', 'running') ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()

    for row in rows:
        payload = _curation_payload_from_row(row)
        if row["status"] == "running":
            _update_curation_run(row["id"], row["tenant_id"], status="pending")
        OperationQueue(DB_PATH).enqueue(
            Operation(
                id=row["id"],
                type=OperationType.CREATE,
                entity_type="curation_run",
                entity_id=row["id"],
                payload=payload,
            ),
            tenant_id=row["tenant_id"],
        )
        _start_curation_worker(row["id"], payload)


def _audit_hook(ctx: MemoryHookContext) -> HookResult | None:
    """Audit hook that logs memory operations."""
    logger.debug("Memory %s: action=%s user=%s", ctx.memory_id, ctx.action, ctx.user_id)
    return None


def _cache_invalidation_hook(ctx: MemoryHookContext) -> HookResult | None:
    """Invalidate caches after memory deletion."""
    if ctx.memory_id:
        try:
            get_hybrid_retriever().invalidate_tfidf_cache(ctx.user_id or "", ctx.tenant_id or "default")
        except Exception:
            logger.exception("Cache invalidation failed for memory %s", ctx.memory_id)
    return None


def _auto_index_embedding_hook(ctx: MemoryHookContext) -> HookResult | None:
    """Auto-index an embedding vector after a memory is stored.

    Best-effort: failures are logged and swallowed so they never break
    the store pipeline.

    Limitation: this hook only fires on store. There are no corresponding
    hooks for update or delete, so the semantic index can diverge silently
    when memories change or are removed. See cubic finding #12.
    """
    if not ctx.memory_id or not ctx.content:
        return None
    try:
        store = get_semantic_search()
        store.index_memory(
            memory_id=ctx.memory_id,
            text=ctx.content,
            user_id=ctx.user_id or USER_ID,
            tenant_id=ctx.tenant_id or "default",
        )
    except Exception:
        logger.debug(
            "Auto-index embedding failed for memory %s: %s",
            ctx.memory_id,
            exc_info=True,
        )
    return None


def _auto_reinforce_hook(ctx: MemoryHookContext) -> HookResult | None:
    """Reinforce a memory's strength when it is retrieved.

    Closes the decay loop: decay weakens memories over time, retrieval
    strengthens them.  Without this hook, activation_count stays at 0
    forever and strength_trend never reflects actual usage.

    Best-effort: failures are logged and swallowed so they never break
    the retrieval pipeline.
    """
    if not ctx.memory_id:
        return None
    try:
        get_decay_model().reinforce_memory(
            memory_id=ctx.memory_id,
            user_id=ctx.user_id or USER_ID,
            tenant_id=ctx.tenant_id or "default",
        )
    except Exception:
        logger.debug(
            "Auto-reinforce failed for memory %s: %s",
            ctx.memory_id,
            exc_info=True,
        )
    return None


def _auto_reinforce_batch(memory_ids: list[str], uid: str, tenant_id: str) -> None:
    """Reinforce multiple memories after a hybrid search or injection.

    Best-effort: failures are logged and swallowed.
    """
    if not memory_ids:
        return
    try:
        model = get_decay_model()
    except Exception:
        logger.debug("Auto-reinforce batch: decay model unavailable")
        return
    for mid in memory_ids:
        try:
            model.reinforce_memory(memory_id=mid, user_id=uid, tenant_id=tenant_id)
        except Exception:
            logger.debug("Auto-reinforce failed for memory %s: %s", mid, exc_info=True)


def _run_decay_sweep() -> int:
    """Run a single Ebbinghaus decay sweep against the Postgres backend.

    Applies exponential decay to all non-ghost memories, updating
    ``current_strength``, ``strength_trend``, and ``last_decay_at``.
    Returns the number of rows updated.
    """
    if _global_backend is None:
        logger.debug("Decay sweep skipped: no backend initialised")
        return 0

    now_iso = datetime.now(timezone.utc).isoformat()

    sql = """
        WITH decay_calc AS (
            SELECT
                id,
                EXTRACT(EPOCH FROM (%s::timestamptz
                    - COALESCE(last_decay_at, created_at)::timestamptz)
                ) / 3600.0 AS hours_elapsed,
                COALESCE(current_strength, importance) AS old_strength,
                activation_count
            FROM memories
            WHERE is_ghost = 0
        )
        UPDATE memories m
        SET
            current_strength = GREATEST(
                0.1,
                dc.old_strength * POWER(0.5, dc.hours_elapsed / 168.0)
            ),
            strength_trend = CASE
                WHEN GREATEST(
                    0.1,
                    dc.old_strength * POWER(0.5, dc.hours_elapsed / 168.0)
                ) <= 0.2 THEN 'stale'
                WHEN dc.activation_count >= 5 THEN 'strengthening'
                WHEN dc.activation_count < 2
                     AND dc.hours_elapsed > 168.0 THEN 'weakening'
                ELSE 'stable'
            END,
            last_decay_at = %s
        FROM decay_calc dc
        WHERE m.id = dc.id
          AND dc.hours_elapsed > 0.000278
    """

    try:
        with _global_backend.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (now_iso, now_iso))
                updated = cur.rowcount
            conn.commit()
        logger.info("Decay sweep complete: %d memories updated", updated)
        return updated
    except Exception:
        logger.exception("Decay sweep failed")
        return 0


def _start_decay_sweep_thread() -> None:
    """Start a daemon thread that runs decay sweeps at regular intervals.

    Interval is configurable via ``FORESIGHT_DECAY_INTERVAL_HOURS`` env var
    (default: 6 hours). The thread catches all exceptions and never dies.
    An initial sweep runs 30 seconds after startup.
    """
    interval_hours = float(os.environ.get("FORESIGHT_DECAY_INTERVAL_HOURS", "6"))

    def _sweep_loop() -> None:
        time.sleep(30)
        while True:
            try:
                _run_decay_sweep()
            except Exception:
                logger.exception("Decay sweep thread caught unexpected exception")
            time.sleep(interval_hours * 3600)

    thread = threading.Thread(target=_sweep_loop, daemon=True, name="foresight-decay-sweep")
    thread.start()
    logger.info("Decay sweep thread started (interval=%sh)", interval_hours)


def _start_maintenance_gc_thread() -> None:
    """Start a daemon thread that runs memory maintenance + GC on a schedule.

    Interval is configurable via ``FORESIGHT_MAINTENANCE_INTERVAL_HOURS`` env
    var (default: 24 hours). The thread catches all exceptions and never dies.
    An initial run starts 60 seconds after startup.

    Maintenance modes run automatically:
    - consolidate: auto-merge near-duplicates (Jaccard >=0.70 overlap)
    - archive_stale: ghost memories with low strength/importance
    - synthesize: emit insight events for recurring topics

    GC phases:
    - Delete memories past retention TTL (ephemeral >24h, short_term >7d)
    - Prune old decay events (>30d) and maintenance events (>60d)
    - Clean orphaned entity links + embeddings

    contradict mode is intentionally excluded — it flags for admin review only
    and should not run unattended.
    """
    interval_hours = float(os.environ.get("FORESIGHT_MAINTENANCE_INTERVAL_HOURS", "24"))
    if not (isinstance(interval_hours, (int, float)) and interval_hours > 0 and math.isfinite(interval_hours)):
        logger.warning(
            "Invalid FORESIGHT_MAINTENANCE_INTERVAL_HOURS=%r, falling back to 24h",
            os.environ.get("FORESIGHT_MAINTENANCE_INTERVAL_HOURS"),
        )
        interval_hours = 24.0

    def _maintenance_loop() -> None:
        time.sleep(60)
        while True:
            try:
                _run_scheduled_maintenance_gc()
            except Exception:
                logger.exception("Maintenance/GC thread caught unexpected exception")
            time.sleep(interval_hours * 3600)

    thread = threading.Thread(target=_maintenance_loop, daemon=True, name="foresight-maintenance-gc")
    thread.start()
    logger.info("Maintenance+GC thread started (interval=%sh)", interval_hours)


def _start_sync_thread() -> None:
    """Start a daemon thread that drains pending sync operations.

    Initialises the global ``SyncManager`` singleton, wires its progress
    callback to publish ``sync.progress`` events on the event bus, and runs
    periodic sync cycles.  Interval is configurable via
    ``FORESIGHT_SYNC_INTERVAL_SECONDS`` (default: 300). The first cycle
    starts 30 seconds after launch so it does not compete with the
    startup surge.
    """
    interval = float(os.environ.get("FORESIGHT_SYNC_INTERVAL_SECONDS", "300"))
    if not (isinstance(interval, (int, float)) and interval > 0 and math.isfinite(interval)):
        logger.warning(
            "Invalid FORESIGHT_SYNC_INTERVAL_SECONDS=%r, falling back to 300s",
            os.environ.get("FORESIGHT_SYNC_INTERVAL_SECONDS"),
        )
        interval = 300.0

    node_id = os.environ.get("FORESIGHT_NODE_ID", "foresight-server")
    mgr = get_sync_manager(node_id=node_id)
    _SERVER_STATE["sync_manager"] = mgr
    mgr.set_online(True)

    # Publish sync.progress events on the event bus
    def _on_progress(progress: SyncProgress) -> None:
        try:
            bus = get_event_bus_with_stream()
            bus.publish(
                sync_progress(
                    status=progress.status.value,
                    pending_count=progress.pending_operations,
                    synced_count=progress.synced_operations,
                    failed_count=len(progress.errors),
                )
            )
        except Exception:
            logger.debug("sync progress publish failed", exc_info=True)

    mgr.on_progress(_on_progress)

    def _sync_loop() -> None:
        time.sleep(30)
        while True:
            try:
                mgr.sync()
            except Exception:
                logger.exception("Sync thread caught unexpected exception")
            time.sleep(interval)

    thread = threading.Thread(target=_sync_loop, daemon=True, name="foresight-sync")
    thread.start()
    logger.info("Sync thread started (interval=%ss, node=%s)", interval, node_id)


def _run_scheduled_maintenance_gc() -> None:
    """Run maintenance + GC for all known (user_id, tenant_id) pairs."""
    if _global_backend is None:
        logger.warning("Maintenance/GC skipped: no backend initialised")
        return

    pairs: list[tuple[str, str, str]] = []
    try:
        with _global_backend.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT user_id, tenant_id, COALESCE(bank_id, 'default') AS bank_id "
                    "FROM memories WHERE is_ghost = 0 OR is_ghost IS NULL"
                )
                rows = cur.fetchall()
                for row in rows:
                    row_dict = dict(row) if not isinstance(row, dict) else row
                    uid = row_dict.get("user_id", "")
                    tid = row_dict.get("tenant_id", "default")
                    bid = row_dict.get("bank_id", "default")
                    if uid:
                        pairs.append((uid, tid, bid))
    except Exception:
        logger.exception("Failed to query user/tenant/bank triples for maintenance")
        return

    ghost_tenants: set[str] = set()
    try:
        with _global_backend.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT tenant_id FROM memories WHERE tenant_id IS NOT NULL")
                for row in cur.fetchall():
                    row_dict = dict(row) if not isinstance(row, dict) else row
                    tid = row_dict.get("tenant_id", "default")
                    if tid:
                        ghost_tenants.add(tid)
    except Exception:
        logger.debug("ghost tenant query failed, falling back to maintenance pairs")
        ghost_tenants = {tid for _, tid, _ in pairs}

    if not pairs:
        logger.info("Maintenance/GC: no active user/tenant/bank triples found, skipping maintenance")
    else:
        logger.info("Maintenance/GC starting for %d user/tenant/bank triple(s)", len(pairs))

    if pairs:
        maintenance_modes = ["consolidate", "archive_stale", "synthesize", "prune_low_relevance", "enhanced_synthesize"]
        job = MemoryMaintenanceJob()
        for uid, tid, bid in pairs:
            try:
                config = MaintenanceConfig(
                    tenant_id=tid,
                    user_id=uid,
                    bank_id=bid,
                    modes=maintenance_modes,
                )
                stats = job.run(config)
                logger.info(
                    "Maintenance complete for user=%s tenant=%s bank=%s: %d dupes found, "
                    "%d auto-merged, %d stale archived, %d insights generated in %.2fs",
                    uid,
                    tid,
                    bid,
                    stats.duplicates_found,
                    stats.duplicates_auto_consolidated,
                    stats.stale_archived,
                    stats.insights_generated,
                    stats.maintenance_duration_seconds,
                )
                try:
                    from .context_blocks import auto_distill_context_blocks

                    distill_res = auto_distill_context_blocks(uid, tid)
                    if distill_res.get("distilled_blocks"):
                        logger.info(
                            "Auto-distilled context blocks for user=%s tenant=%s: %s",
                            uid,
                            tid,
                            distill_res["distilled_blocks"],
                        )
                except Exception:
                    logger.debug("Auto-distill context blocks failed (non-fatal)", exc_info=True)
            except Exception:
                logger.exception("Maintenance failed for user=%s tenant=%s bank=%s", uid, tid, bid)

    seen_tenants: set[str] = set()
    for _, tid, _ in pairs:
        if tid in seen_tenants:
            continue
        seen_tenants.add(tid)
        try:
            gc_stats = run_memory_gc(tenant_id=tid)
            logger.info(
                "GC complete for tenant=%s: %d expired deleted, "
                "%d decay events pruned, %d maintenance events pruned, "
                "%d orphan links cleaned, %d orphan embeddings cleaned in %.2fs",
                tid,
                gc_stats.expired_memories_deleted,
                gc_stats.decay_events_pruned,
                gc_stats.maintenance_events_pruned,
                gc_stats.orphan_links_cleaned,
                gc_stats.orphan_embeddings_cleaned,
                gc_stats.gc_duration_seconds,
            )
        except Exception:
            logger.exception("GC failed for tenant=%s", tid)

    edge_tenants: set[str] = set()
    for _, tid, _ in pairs:
        if tid in edge_tenants:
            continue
        edge_tenants.add(tid)
        try:
            decay_stats = run_edge_decay_update(tid)
            logger.info(
                "Edge decay for tenant=%s: %d edges updated, avg decay %.3f",
                tid,
                decay_stats.edges_processed,
                decay_stats.avg_decay_factor,
            )
            prune_stats = run_edge_pruning(tid)
            if prune_stats.edges_pruned:
                logger.info(
                    "Edge pruning for tenant=%s: %d stale edges pruned",
                    tid,
                    prune_stats.edges_pruned,
                )
        except Exception:
            logger.exception("Edge decay/prune failed for tenant=%s", tid)

    for tid in ghost_tenants:
        try:
            ghost_stats = run_ghost_cleanup(tid)
            logger.info(
                "Ghost cleanup for tenant=%s: %d/%d deleted, ~%d bytes freed in %.2fs",
                tid,
                ghost_stats.ghost_memories_deleted,
                ghost_stats.ghost_memories_found,
                ghost_stats.bytes_freed,
                ghost_stats.cleanup_duration_seconds,
            )
        except Exception:
            logger.exception("Ghost cleanup failed for tenant=%s", tid)

    logger.info("Maintenance/GC cycle complete")


def main(host: str | None = None, port: int | None = None) -> None:
    """Start the Foresight MCP server.

    Args:
        host: Host to bind when using SSE transport. If None, uses FORESIGHT_HOST
              env var or defaults to '127.0.0.1'.
        port: Port to bind when using SSE transport. If None, checks FORESIGHT_PORT
              env var. If neither is set, runs in stdio mode (default).
    """
    _SERVER_STATE["started_at"] = time.time()
    init_db()

    # Create and connect the database backend (PIX-3994)
    _initialize_backend()

    # Initialize Redis companion (PIX-3995) — must come after backend init
    _initialize_redis()

    # Eagerly initialise services with the backend so lazy singletons
    # already have it when tool handlers call get_*() without args.
    if _global_backend is not None:
        get_hybrid_retriever(backend=_global_backend)
        get_graph_store(backend=_global_backend)
        get_temporal_query_builder(backend=_global_backend)

    _resume_pending_curation_runs()
    _start_decay_sweep_thread()
    _start_maintenance_gc_thread()
    _start_sync_thread()

    reg = get_memory_hook_registry()
    reg.register(MemoryHookType.PRE_STORE, _audit_hook, name="audit")
    reg.register(MemoryHookType.POST_STORE, _audit_hook, name="audit")
    reg.register(MemoryHookType.PRE_RETRIEVE, _audit_hook, name="audit")
    reg.register(MemoryHookType.POST_RETRIEVE, _audit_hook, name="audit")
    reg.register(MemoryHookType.PRE_UPDATE, _audit_hook, name="audit")
    reg.register(MemoryHookType.POST_UPDATE, _audit_hook, name="audit")
    reg.register(MemoryHookType.PRE_DELETE, _audit_hook, name="audit")
    reg.register(MemoryHookType.POST_DELETE, _audit_hook, name="audit")
    reg.register(MemoryHookType.POST_DELETE, _cache_invalidation_hook, name="cache_invalidation")
    reg.register(MemoryHookType.POST_STORE, _auto_index_embedding_hook, name="auto_index_embedding")
    reg.register(MemoryHookType.POST_RETRIEVE, _auto_reinforce_hook, name="auto_reinforce")

    # Initialize WebSocket server
    async def websocket_auth_callback(token: str) -> tuple[str, str] | None:
        """Auth callback for WebSocket connections.

        Validates the token via the real AuthManager — first as an API key,
        then as a session ID — and returns (user_id, tenant_id) on success.
        """
        if not token:
            return None

        auth_manager = get_auth_manager()

        # Try API key authentication first
        user = auth_manager.authenticate_api_key(token)

        # Fall back to session token validation
        if user is None:
            user = auth_manager.validate_session(token)

        if user is None:
            return None

        # Resolve tenant: use "default" if user has access to all tenants,
        # otherwise use the first allowed tenant
        if not user.tenant_access:
            tenant_id = "default"
        elif "default" in user.tenant_access:
            tenant_id = "default"
        else:
            tenant_id = user.tenant_access[0]

        if not auth_manager.validate_user_tenant_access(user, tenant_id):
            return None

        return user.user_id, tenant_id

    # WebSocket server is opt-in: only enabled when FORESIGHT_ENABLE_WS is set.
    # The WS surface exists for live UI/inspector clients; stdio MCP tools do not
    # need it. Skipping avoids binding port 8765 and re-broadcasting port-forward
    # notices on every tool call.
    if os.environ.get("FORESIGHT_ENABLE_WS"):
        websocket_server = WebSocketServer(
            event_bus=get_event_bus(),
            auth_callback=cast(Any, websocket_auth_callback),
        )

        def _run_ws_in_thread():
            asyncio.run(websocket_server.start())

        ws_thread = threading.Thread(target=_run_ws_in_thread, daemon=True, name="foresight-websocket")
        ws_thread.start()

    # Determine transport: streamable-http (full HTTP, stateless request/response) when port is set, stdio otherwise.
    # stateless_http=True keeps the server stateless: HTTP MCP clients (e.g. the Copilot CLI transport) cache the
    # Mcp-Session-Id from the initial handshake and never re-handshake, so any server restart that wipes the
    # in-memory session dict would strand the client with 404 "Session expired" errors until the client is
    # restarted. Stateless mode skips session tracking entirely, making server restarts safe.
    transport_port = port if port is not None else os.environ.get("FORESIGHT_PORT")
    if transport_port is not None:
        transport_host = host if host is not None else os.environ.get("FORESIGHT_HOST", "127.0.0.1")
        mcp.run(
            transport="streamable-http",
            host=transport_host,
            port=int(transport_port),
            stateless_http=True,
            show_banner=False,
        )
    else:
        mcp.run(show_banner=False)


# =============================================================================
# Multi-Tenant Isolation Functions
# =============================================================================


@dataclass
class TenantContext:
    """Tenant context for isolation."""

    tenant_id: str
    rate_limit: int = 100
    burst_limit: int = 20


def get_tenant_context() -> dict:
    """Get current tenant context."""
    if _SERVER_STATE["tenant_context"] is None:
        _SERVER_STATE["tenant_context"] = {"id": get_current_account_id()}
    return _SERVER_STATE["tenant_context"]


def set_tenant_context(tenant_id: str) -> None:
    """Set tenant context for current request.

    Deprecated: Use set_current_tenant_id() from tenant_module instead.
    The TenantMiddleware handles per-request tenant isolation automatically.
    """
    warnings.warn(
        "set_tenant_context() is deprecated; use set_current_tenant_id() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    set_current_tenant_id(tenant_id)


def switch_tenant(tenant_id: str) -> str:
    """
    Switch current tenant context.

    Args:
        tenant_id: Tenant to switch to

    Returns:
        Confirmation message
    """
    conn = get_db_connection()
    try:
        if tenant_id == DEFAULT_TENANT_ID:
            _seed_default_tenant(conn)
            conn.commit()
            set_current_tenant_id(tenant_id)
            return f"Switched to tenant '{tenant_id}'"

        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if not row:
            return f"Tenant '{tenant_id}' not found"
    finally:
        conn.close()

    set_current_tenant_id(tenant_id)
    return f"Switched to tenant '{tenant_id}'"


# =============================================================================
# Temporal Memory Tools
# =============================================================================

# =============================================================================
# Temporal and Status Tools
# =============================================================================


@mcp.tool(output_schema=None)
def query_memories_temporal(
    options: TemporalWindow | None = None,
    user_id: str | None = None,
    window: Literal["today", "week", "month", "year"] | None = None,
    trend: str | None = None,
    category: str | None = None,
    limit: int | None = None,
) -> str:
    """
    Query memories based on temporal signals (window, trend).

    Args:
        options: Temporal query parameters (window, trend, category, limit)
        user_id: Optional user ID override
        window: Flat parameter for time window (optional)
        trend: Flat parameter for trend filter (optional)
        category: Flat parameter for category filter (optional)
        limit: Flat parameter for max results (optional)
    """
    if options is None:
        kwargs = {}
        if window is not None:
            kwargs["window"] = window
        if trend is not None:
            kwargs["trend"] = trend
        if category is not None:
            kwargs["category"] = category
        if limit is not None:
            kwargs["limit"] = limit
        options = TemporalWindow(**kwargs)

    uid = user_id or USER_ID
    builder = get_temporal_query_builder()

    if options.trend:
        results = builder.get_memories_by_trend(
            user_id=uid, trend=options.trend, limit=options.limit, category=options.category
        )
    else:
        results = builder.get_memories_from_window(
            user_id=uid, window=options.window, limit=options.limit, min_importance=0.1, category=options.category
        )

    return json.dumps(
        [
            {
                "memory_id": r.memory_id,
                "content": r.content,
                "importance": r.importance,
                "strength_trend": r.strength_trend,
                "activation_count": r.activation_count,
                "created_at": r.created_at,
                "category": r.category,
            }
            for r in results
        ],
        indent=2,
    )


@mcp.tool(output_schema=None)
def get_system_status(
    options: SystemStatusOptions | None = None,
    user_id: str | None = None,
    include_trends: bool | None = None,
    timeframe: str | None = None,
    include_cache_metrics: bool | None = None,
    enforce_hard_caps: bool | None = None,
) -> str:
    """
    Get system health, memory statistics, and temporal trends.
    Args:
        options: Optional status and trend parameters
        user_id: Optional user ID override
        include_trends: Flat parameter for including trends (optional)
        timeframe: Flat parameter for timeframe (optional)
        include_cache_metrics: Flat parameter for including cache metrics (optional)
        enforce_hard_caps: Flat parameter for enforcing hard caps (optional)
    """
    uid = user_id or USER_ID
    if options is None:
        kwargs = {}
        if include_trends is not None:
            kwargs["include_trends"] = include_trends
        if timeframe is not None:
            kwargs["timeframe"] = timeframe
        if include_cache_metrics is not None:
            kwargs["include_cache_metrics"] = include_cache_metrics
        if enforce_hard_caps is not None:
            kwargs["enforce_hard_caps"] = enforce_hard_caps
        opts = SystemStatusOptions(**kwargs)
    else:
        opts = options
    tenant_id = get_current_account_id()
    conn = get_db_connection()
    # Basic stats
    count = (
        conn.execute("SELECT COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ?", (uid, tenant_id)).fetchone()
        or (0,)
    )[0]
    scope_counts = conn.execute(
        "SELECT scope, COUNT(*) AS scope_count FROM memories WHERE user_id = ? AND tenant_id = ? GROUP BY scope",
        (uid, tenant_id),
    ).fetchall()
    # Maintenance stats (lightweight — uses existing open conn)
    try:
        maintenance_rows = conn.execute(
            "SELECT event_type, COUNT(*) as cnt FROM events WHERE event_type LIKE ? GROUP BY event_type",
            ("maintenance%",),
        ).fetchall()
        last_maint = (
            conn.execute(
                "SELECT MAX(timestamp) FROM events WHERE event_type LIKE ?",
                ("maintenance%",),
            ).fetchone()
            or (None,)
        )[0]
        by_event_type = {r["event_type"]: r["cnt"] for r in maintenance_rows}
        total_events = sum(by_event_type.values())
        reviews = sum(v for k, v in by_event_type.items() if "maintenance_review" in k)
        insights = by_event_type.get("maintenance_insight", 0)
        maintenance_stats = {
            "total_events": total_events,
            "reviews_flagged": reviews,
            "insights_generated": insights,
            "last_maintenance_run": last_maint,
            "by_type": by_event_type,
        }
    except Exception:
        maintenance_stats = {"error": "Cannot query maintenance events"}

    # Last capture time (lightweight — uses existing open conn)
    try:
        last_cap = (
            conn.execute(
                "SELECT MAX(created_at) FROM memories WHERE user_id = ? AND tenant_id = ?",
                (uid, tenant_id),
            ).fetchone()
            or (None,)
        )[0]
        last_capture_time = last_cap
    except Exception:
        last_capture_time = None

    # Stale/decayed memory count (PIX-3955)
    try:
        stale_count = (
            conn.execute(
                "SELECT COUNT(*) FROM memories WHERE user_id = ? AND tenant_id = ? AND strength_trend = 'stale'",
                (uid, tenant_id),
            ).fetchone()
            or (0,)
        )[0]
    except Exception:
        stale_count = 0

    # Category breakdown (PIX-3955)
    try:
        category_rows = conn.execute(
            "SELECT category, COUNT(*) AS category_count FROM memories WHERE user_id = ? AND tenant_id = ? GROUP BY category",
            (uid, tenant_id),
        ).fetchall()
        by_category = {row["category"]: row["category_count"] for row in category_rows}
    except Exception:
        by_category = {}

    conn.close()
    result = {
        "status": "healthy",
        "memory_count": count,
        "by_scope": {row["scope"]: row["scope_count"] for row in scope_counts},
        "stale_count": stale_count,
        "by_category": by_category,
        "last_injection": dict(_last_injection_stats) if _last_injection_stats else None,
        "tenant_id": tenant_id,
        "maintenance_stats": maintenance_stats,
        "last_capture_time": last_capture_time,
        "payload_budget": {
            "default_lane_weights": {k.name.lower(): v for k, v in DEFAULT_LANE_WEIGHTS.items()},
            "min_lane_chars": MIN_LANE_CHARS,
        },
    }
    # Onboarding hint when the store is empty — gives agents a self-bootstrap path
    # so a fresh install doesn't just silently return nothing on first use.
    if count == 0:
        result["onboarding"] = {
            "status": "empty",
            "hint": (
                "Memory store is empty. Three ways to start: "
                "(1) Set standing preferences — manage_context_blocks(action='update', "
                "label='user_preferences', content='<your preferences>'). "
                "(2) Store a fact or decision — manage_memories(action='store', "
                "content='<what to remember>', store_options={'category': 'preference', "
                "'scope': 'trait', 'retention': 'long_term', 'importance': 0.8}). "
                "(3) Process a conversation — process_session_transcript(session_id='...', messages=[...])."
            ),
        }
    # Add temporal stats/trends if requested
    if opts.include_trends:
        builder = get_temporal_query_builder()
        service = get_temporal_service()
        result["temporal_stats"] = service.get_memory_stats(user_id=uid)
        result["trend_analysis"] = builder.analyze_trends(user_id=uid, timeframe=opts.timeframe)
    # Add cache and budget metrics if requested
    if opts.include_cache_metrics:
        # Memory budget status
        result["memory_budget"] = {
            "current_count": count,
            "max_per_tenant": DEFAULT_MAX_MEMORY_PER_TENANT,
            "utilization_pct": round((count / DEFAULT_MAX_MEMORY_PER_TENANT) * 100, 2),
            "hard_cap_enforced": opts.enforce_hard_caps,
        }
        # Narrative cache metrics (persistent)
        try:
            narrative_cache = get_narrative_cache()
            cache_stats = narrative_cache.stats()
            result["cache_metrics"] = {
                "narrative_cache": {
                    "type": "persistent",
                    "size": cache_stats["size"],
                    "max_entries": cache_stats["max_entries"],
                    "ttl_seconds": cache_stats["ttl_seconds"],
                    "hits": cache_stats["hits"],
                    "misses": cache_stats["misses"],
                    "evictions": cache_stats["eviction_count"],
                    "utilization_pct": round((cache_stats["size"] / cache_stats["max_entries"]) * 100, 2),
                },
                "reflection_narrative_in_process_cache": {
                    "type": "in_process",
                    "size": len(_reflection_narrative_cache),
                },
            }
        except Exception:
            result["cache_metrics"] = {
                "narrative_cache": {"type": "persistent", "error": "Unable to retrieve narrative cache metrics"},
                "reflection_narrative_in_process_cache": {
                    "type": "in_process",
                    "size": len(_reflection_narrative_cache),
                },
            }
        # TF-IDF cache metrics
        try:
            retriever = get_hybrid_retriever()
            tfidf_cache_size = len(retriever._tfidf_cache)
            result["cache_metrics"]["tfidf_cache"] = {
                "size": tfidf_cache_size,
                "max_size": DEFAULT_MAX_TFIDF_CACHE_SIZE,
                "utilization_pct": round((tfidf_cache_size / DEFAULT_MAX_TFIDF_CACHE_SIZE) * 100, 2),
            }
            # Retrieval debug info
            result["retrieval_debug"] = {
                "tfidf_cache_size": tfidf_cache_size,
                "fast_path_enabled": True,
            }
        except Exception:
            result["cache_metrics"]["tfidf_cache"] = {"error": "Unable to retrieve TF-IDF cache metrics"}
    from .encryption import get_encryption_engine
    result["encryption"] = get_encryption_engine().get_status().to_dict()
    return json.dumps(result, indent=2)


@mcp.tool(output_schema=None)
def manage_encryption(
    action: Literal["status", "rotate_key", "encrypt_all"] = "status",
    old_key: str | None = None,
    new_key: str | None = None,
    user_id: str | None = None,
) -> str:
    """Manage the optional AES-256-GCM encryption layer for stored memories.

    Actions:
    - 'status': Check if encryption is enabled, active algorithm, and mode.
    - 'rotate_key': Re-encrypt all existing memories under a new master key.
    - 'encrypt_all': Retroactively encrypt all plaintext memories in storage.
    """
    uid = user_id or USER_ID
    tenant_id = get_current_account_id()
    engine = get_encryption_engine()

    if action == "status":
        return json.dumps(engine.get_status().to_dict(), indent=2)

    if action == "encrypt_all":
        if not engine.enabled:
            return "Error: No encryption key configured. Set FORESIGHT_ENCRYPTION_KEY first."
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT id, content FROM memories WHERE user_id = ? AND tenant_id = ?",
                (uid, tenant_id),
            ).fetchall()
            count = 0
            for r in rows:
                content = r["content"] or ""
                if not engine.is_encrypted(content):
                    enc = engine.encrypt(content, tenant_id=tenant_id, user_id=uid, force=True)
                    conn.execute("UPDATE memories SET content = ? WHERE id = ?", (enc, r["id"]))
                    count += 1
            conn.commit()
            return f"Encrypted {count} memories with AES-256-GCM."
        finally:
            conn.close()

    if action == "rotate_key":
        if not old_key or not new_key:
            return "Error: old_key and new_key are both required for rotate_key action."
        conn = get_db_connection()
        try:
            res = engine.rotate_key(
                old_master_key=old_key,
                new_master_key=new_key,
                conn=conn,
                tenant_id=tenant_id,
                user_id=uid,
            )
            return json.dumps(res, indent=2)
        finally:
            conn.close()

    return f"Unknown action: {action}"


# ─── Auto-injection: behavioral instruction prompt ────────────────────────────
# This prompt tells the LLM *how* to use inject_context automatically — when
# to call it, why, and what to do with the result.  Clients that include server
# prompts in the system message (Claude Code, Cursor, Goose, etc.) will pick
# this up and the LLM will start calling inject_context proactively without the
# user ever having to ask.
# ──────────────────────────────────────────────────────────────────────────────


@mcp.prompt(
    name="foresight_autocontext",
    title="Foresight — automatic context injection",
    description=(
        "System instructions that make the assistant call inject_context "
        "automatically at conversation start and on topic shifts, so prior "
        "memories and context blocks surface without the user having to ask."
    ),
)
def _prompt_autocontext() -> str:
    """Return the behavioral instruction that drives automatic memory retrieval and storage.

    When a client includes this prompt in the system message, the LLM will call
    inject_context on its own — at conversation start, on topic shifts, and on
    recall signals — and will proactively store preferences, decisions, and facts
    as it learns them. Both halves together mean memory accumulates from day one
    without the user ever having to ask.
    """
    return (
        "You have access to the Foresight persistent memory system.\n\n"
        "## Automatic context retrieval — follow without being asked\n\n"
        "1. **At conversation start**: Before your first reply, call `inject_context` "
        "with the user's opening message. Apply what comes back silently.\n\n"
        "2. **On topic shift**: New subject, project, person, or task → call "
        "`inject_context` again before replying.\n\n"
        "3. **On recall signals**: 'Last time', 'as we discussed', 'you mentioned', "
        "'remember when' → call `inject_context` immediately.\n\n"
        "4. **Before non-trivial actions**: Before writing code, making a plan, or "
        "giving advice on a complex request, call `inject_context` first.\n\n"
        "## How to use retrieved context\n\n"
        "- Incorporate relevant memories into your answer naturally — no quoting.\n"
        "- Treat guidance and preferences found in context blocks as standing "
        "instructions (tone, format, constraints, coding style).\n"
        "- If nothing comes back, proceed normally — never mention the absence.\n"
        "- Never announce that you called `inject_context`.\n\n"
        "## Proactive memory storage — follow without being asked\n\n"
        "5. **Capture stated preferences immediately**: When the user says how they "
        "want things done ('I prefer X', 'always use Y', 'don't do Z'), call "
        "`manage_context_blocks(action='update', label='user_preferences', "
        "content='<preference>')` to append it. Do this silently.\n\n"
        "6. **Store decisions and key facts**: When a non-trivial decision is made "
        "or an important fact surfaces, call `manage_memories(action='store', "
        "content='<concise summary>', store_options={'category': 'decision', "
        "'scope': 'arc', 'retention': 'long_term', 'importance': 0.7})`. "
        "One clear sentence per memory — never store the raw transcript.\n\n"
        "7. **Track open tasks**: When a task is raised but not finished this session, "
        "add it to `pending_items` via `manage_context_blocks(action='update', "
        "label='pending_items', content='<task>')`.\n\n"
        "8. **At session end**: When the conversation concludes, call "
        "`process_session_transcript(session_id='<date>-<user>', messages=[...])`  "
        "to extract and persist everything learned. Omit `project_path` — the server "
        "detects it from git automatically.\n\n"
        "## Storage rules\n\n"
        "- Store the gist, not the transcript. One fact per memory.\n"
        "- Never announce that you are storing something unless the user asks.\n"
        "- Skip trivial chitchat — only store what would be worth knowing next session.\n"
        "- Phrase triggers work automatically: if the user types 'remember this:', "
        "'decision:', or 'preference:', the server captures it without any tool call."
    )


# ──────────────────────────────────────────────────────────────────────────────


def memory_status(
    user_id: str | None = None,
    include_trends: bool = False,
    timeframe: str = "30 days",
) -> str:
    """Legacy alias for get_system_status() used by CLI health checks."""
    return get_system_status(
        options=SystemStatusOptions(include_trends=include_trends, timeframe=timeframe),
        user_id=user_id,
    )


def store_memory(
    content: str,
    user_id: str | None = None,
    category: str = "fact",
    scope: str = "session",
    retention: str = "short_term",
    importance: float = 0.5,
    emotional_context: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    relation_type: str | None = None,
    related_memory_id: str | None = None,
) -> str:
    """Legacy alias for manage_memories(action="store") used by callers and tests."""
    options = MemoryAction(
        action="store",
        content=content,
        options=MemoryOptions(
            category=category,
            scope=scope,
            retention=retention,
            importance=importance,
            emotional_context=emotional_context,
            metrics=metrics,
            relation_type=relation_type,
            related_memory_id=related_memory_id,
        ),
    )
    return manage_memories(options, user_id=user_id)


def list_memories(limit: int = 10, offset: int = 0, user_id: str | None = None) -> str:
    """Legacy alias for search_memories(query_type="list")."""
    options = SearchOptions(query_type="list", limit=limit, offset=offset)
    return search_memories(options, user_id=user_id)


def query_memories(
    query: str,
    user_id: str | None = None,
    limit: int = 10,
    use_hybrid: bool = True,
    min_importance: float = 0.1,
    offset: int = 0,
) -> str:
    """Legacy alias for search_memories(query_type="keyword")."""
    options = SearchOptions(
        query_type="keyword",
        query=query,
        limit=limit,
        use_hybrid=use_hybrid,
        min_importance=min_importance,
        offset=offset,
    )
    return search_memories(options, user_id=user_id)


def get_memory(memory_id: str, user_id: str | None = None, min_importance: float = 0.1) -> str:
    """Legacy alias for search_memories(query_type="id")."""
    options = SearchOptions(query_type="id", memory_id=memory_id, min_importance=min_importance)
    return search_memories(options, user_id=user_id)


def update_memory(
    memory_id: str,
    user_id: str | None = None,
    content: str | None = None,
    category: str | None = None,
    scope: str | None = None,
    retention: str | None = None,
    tags: list[str] | None = None,
    relation_type: str | None = None,
    related_memory_id: str | None = None,
) -> str:
    """Legacy alias for manage_memories(action="update")."""
    updates = MemoryUpdateOptions(
        content=content,
        category=category,
        scope=scope,
        retention=retention,
        tags=tags,
        relation_type=relation_type,
        related_memory_id=related_memory_id,
    )
    options = MemoryAction(action="update", memory_id=memory_id, updates=updates)
    return manage_memories(options, user_id=user_id)


def delete_memory(memory_id: str, user_id: str | None = None) -> str:
    """Legacy alias for manage_memories(action="delete")."""
    options = MemoryAction(action="delete", memory_id=memory_id)
    return manage_memories(options, user_id=user_id)


def archive_memory(memory_id: str, user_id: str | None = None) -> str:
    """Legacy alias for manage_memories(action="archive")."""
    options = MemoryAction(action="archive", memory_id=memory_id)
    return manage_memories(options, user_id=user_id)


# =============================================================================
# Profile Synthesis Tool
# =============================================================================


def synthesize_profile(
    user_id: str | None = None,
    max_static_memories: int = 20,
    max_dynamic_memories: int = 10,
    include_synthesis: bool = True,
    format_prompt: bool = False,
) -> str:
    """
    Build a user profile with static (stable facts) and dynamic (recent context) layers.

    Profile = compact summary of who the user is (static) and what they are
    currently working on (dynamic). Directly injectable into system prompts.

    Args:
        user_id: Optional user ID override.
        max_static_memories: Max trait/fact memories to consider.
        max_dynamic_memories: Max session/arc memories to consider.
        include_synthesis: Run enhanced synthesis for contradiction detection.
        format_prompt: Return as a formatted prompt block instead of JSON.

    Returns:
        JSON:  ``{"static": [...], "dynamic": [...]}``
        Prompt block when ``format_prompt=True``.
    """
    uid = user_id or USER_ID
    cfg = ProfileConfig(
        max_static_memories=max_static_memories,
        max_dynamic_memories=max_dynamic_memories,
        include_synthesis=include_synthesis,
    )
    profile = _run_async(_synthesize_profile(uid, get_current_account_id(), cfg))

    if format_prompt:
        return profile_to_prompt(profile)

    return json.dumps(profile.to_dict(), indent=2, ensure_ascii=False)


# =============================================================================
# Entity and Graph Tools
# =============================================================================


def manage_entities(action: EntityAction, user_id: str | None = None) -> str:
    """
    Manage entities and relationships (extract, link).

    Args:
        action: Action details (extract, link)
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    store = get_graph_store()

    if action.action == "extract":
        if not action.content:
            return "Content is required for entity extraction"
        extractor = get_entity_extractor()
        result = _run_async(extractor.extract(action.content))
        return json.dumps(
            {
                "user_id": uid,
                "entities": [e.to_dict() for e in result.entities],
                "relationships": [r.to_dict() for r in result.relationships],
            },
            indent=2,
        )

    if action.action == "link":
        if not action.memory_id or not action.entity_ids:
            return "memory_id and entity_ids are required for linking"
        store.link_memory_to_entities(action.memory_id, action.entity_ids, uid)
        return f"Linked memory {action.memory_id} to {len(action.entity_ids)} entities"

    return "Invalid action"


def _handle_entity_query_by_type(uid: str, store, query: EntityQuery) -> str:
    """Helper for entity query by type."""
    if not query.entity_type:
        return "entity_type is required"
    entities = store.get_entities_by_type(uid, query.entity_type, query.limit)
    return json.dumps([e.to_dict() for e in entities], indent=2)


def _handle_entity_query_by_name(uid: str, store, query: EntityQuery) -> str:
    """Helper for entity query by name."""
    if not query.name:
        return "name is required"
    entities = store.find_entities_by_name(uid, query.name, query.limit)
    return json.dumps([e.to_dict() for e in entities], indent=2)


def _handle_entity_query_relationships(uid: str, store, query: EntityQuery) -> str:
    """Helper for entity relationships query."""
    if not query.entity_id:
        return "entity_id is required"
    relationships = store.get_relationships(query.entity_id, uid, query.direction)
    return json.dumps([r.to_dict() for r in relationships], indent=2)


def query_entities(query: EntityQuery, user_id: str | None = None) -> str:
    """
    Query entities and graph relationships.

    Args:
        query: Query parameters (by_type, by_name, relationships, traverse)
        user_id: Optional user ID override
    """
    uid = user_id or USER_ID
    store = get_graph_store()

    if query.query_type == "by_type":
        return _handle_entity_query_by_type(uid, store, query)

    if query.query_type == "by_name":
        return _handle_entity_query_by_name(uid, store, query)

    if query.query_type == "relationships":
        return _handle_entity_query_relationships(uid, store, query)

    if query.query_type == "traverse":
        if not query.entity_id:
            return "entity_id is required for traversal"
        result = store.traverse_graph(query.entity_id, uid, query.max_depth, query.limit)
        return json.dumps(
            {
                "nodes": [e.to_dict() for e in result.nodes],
                "edges": [r.to_dict() for r in result.edges],
            },
            indent=2,
        )

    return "Invalid query_type"


# =============================================================================
# Document Layer Tools (MEM-7)
# =============================================================================


def create_document(
    title: str,
    content: str,
    user_id: str | None = None,
    source: str = "note",
    metadata: dict[str, Any] | None = None,
    char_budget: int = _DOC_CHUNK_BUDGET,
    memory_id_for_chunk: str | None = None,
) -> str:
    """
    Persist a raw source document and its extracted chunks.

    Separates source content from derived memories so that extraction
    can be re-run without losing the original. Chunks are produced
    synchronously with paragraph-based splitting (see chunk_text).

    Args:
        title: Human-readable document title.
        content: Raw source text.
        user_id: Optional user ID override.
        source: One of transcript/article/journal/note/email/other.
        metadata: Optional JSON-serializable metadata.
        char_budget: Soft max chars per chunk (100-8000).
        memory_id_for_chunk: If set, applied to every chunk produced.
    """
    uid = user_id or USER_ID
    store = get_document_store()
    try:
        doc, chunks = store.create_document(
            title=title,
            content=content,
            user_id=uid,
            options=DocumentCreateOptions(
                source=source,
                tenant_id=None,  # server.py doesn't handle tenant_id, so use None
                metadata=metadata,
                char_budget=char_budget,
                memory_id_for_chunk=memory_id_for_chunk,
            ),
        )
    except DocumentLayerError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {
            "document": doc.to_dict(),
            "chunks": [c.to_dict() for c in chunks],
        },
        indent=2,
    )


def get_document(
    document_id: str,
    user_id: str | None = None,
) -> str:
    """Fetch a stored document by ID."""
    uid = user_id or USER_ID
    store = get_document_store()
    try:
        doc = store.get_document(document_id=document_id, user_id=uid)
    except DocumentLayerError as exc:
        return f"Error: {exc}"
    if doc is None:
        return f"Document {document_id} not found."
    return json.dumps(doc.to_dict(), indent=2)


def list_document_chunks(
    document_id: str,
    user_id: str | None = None,
) -> str:
    """List all chunks produced from a document, in order."""
    uid = user_id or USER_ID
    store = get_document_store()
    try:
        chunks = store.list_chunks(document_id=document_id, user_id=uid)
    except DocumentLayerError as exc:
        return f"Error: {exc}"
    return json.dumps([c.to_dict() for c in chunks], indent=2)


def get_memory_source(
    memory_id: str,
    user_id: str | None = None,
) -> str:
    """Reverse-lookup: given a memory_id, return its source document + chunk."""
    uid = user_id or USER_ID
    store = get_document_store()
    try:
        result = store.get_memory_source(memory_id=memory_id, user_id=uid)
    except DocumentLayerError as exc:
        return f"Error: {exc}"
    if result is None:
        return f"No source document found for memory {memory_id}."
    doc, chunk = result
    return json.dumps(
        {"document": doc.to_dict(), "chunk": chunk.to_dict()},
        indent=2,
    )


def delete_document(
    document_id: str,
    user_id: str | None = None,
) -> str:
    """Delete a document and all of its chunks (CASCADE)."""
    uid = user_id or USER_ID
    store = get_document_store()
    try:
        deleted = store.delete_document(document_id=document_id, user_id=uid)
    except DocumentLayerError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {"document_id": document_id, "deleted": deleted},
        indent=2,
    )


# =============================================================================
# Clustering Tools (PIX-3841)
# =============================================================================


def _fetch_memories_for_clustering(
    uid: str,
    tenant_id: str,
    limit: int = 10_000,
) -> list[dict[str, Any]]:
    """Fetch non-ghost memories for a user/tenant for clustering.

    Returns dicts with id, content, user_id, tenant_id matching
    the input format expected by ``cluster_memories()``.
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """SELECT id, content, user_id, tenant_id
               FROM memories
               WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0
               ORDER BY created_at DESC
               LIMIT ?""",
            (uid, tenant_id, limit),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "content": row["content"],
                "user_id": row["user_id"],
                "tenant_id": row["tenant_id"] or tenant_id,
            }
            for row in rows
        ]
    finally:
        conn.close()


def _upsert_cluster_results(
    result: ClusterResult,
    uid: str,
    tenant_id: str,
) -> dict[str, Any]:
    """Upsert cluster entities and link memories into the graph store.

    Returns a summary dict with entity_count and link_count.
    """
    store = get_graph_store()

    entity_count = 0
    for entity_dict in result.cluster_entities:
        entity = Entity(
            id=entity_dict["id"],
            name=entity_dict["name"],
            entity_type=entity_dict["entity_type"],  # "cluster"
            description=entity_dict.get("description"),
            properties=entity_dict.get("properties", {}),
        )
        store.upsert_entity(entity, uid, tenant_id=tenant_id)
        entity_count += 1

    # Group memory links by memory_id for batch linking
    memory_groups: dict[str, dict[str, Any]] = {}
    for link in result.memory_links:
        mid = link["memory_id"]
        if mid not in memory_groups:
            memory_groups[mid] = {"entity_ids": [], "scores": {}}
        memory_groups[mid]["entity_ids"].append(link["entity_id"])
        memory_groups[mid]["scores"][link["entity_id"]] = link.get("relevance_score", 1.0)

    link_count = 0
    for mid, group in memory_groups.items():
        store.link_memory_to_entities(
            mid,
            group["entity_ids"],
            uid,
            scores=group["scores"],
            tenant_id=tenant_id,
        )
        link_count += len(group["entity_ids"])

    return {
        "entity_count": entity_count,
        "link_count": link_count,
        "cluster_count": entity_count,
    }


def run_clustering(
    user_id: str | None = None,
    min_similarity: float = 0.25,
    min_cluster_size: int = 2,
    max_clusters: int | None = 20,
) -> str:
    """Group memories into semantic clusters using token-set Jaccard similarity.

    Clusters are created as entities in the graph store and memories are
    linked to their respective clusters. Can be called directly or triggered
    as part of a curation run.

    The implementation uses token-set Jaccard similarity as a cheap
    stand-in for semantic distance and merges the densest pairs greedily.

    Args:
        user_id: Optional user ID override.
        min_similarity: Minimum Jaccard similarity to consider (default 0.25).
        min_cluster_size: Minimum memories per cluster (default 2).
        max_clusters: Maximum clusters to create (default 20, None for unlimited).

    Returns:
        JSON summary of the clustering operation.
    """
    uid = user_id or USER_ID
    tenant_id = get_current_account_id()

    memories = _fetch_memories_for_clustering(uid, tenant_id)
    if not memories:
        return json.dumps(
            {"ok": True, "clusters_created": 0, "memories_processed": 0, "message": "No memories to cluster."},
            indent=2,
        )

    result = cluster_memories(
        memories,
        min_similarity=min_similarity,
        min_cluster_size=min_cluster_size,
        max_clusters=max_clusters,
    )

    if not result.cluster_entities:
        return json.dumps(
            {
                "ok": True,
                "clusters_created": 0,
                "memories_processed": len(memories),
                "message": "No clusters met the similarity threshold.",
            },
            indent=2,
        )

    summary = _upsert_cluster_results(result, uid, tenant_id)
    return json.dumps(
        {
            "ok": True,
            "clusters_created": summary["cluster_count"],
            "memories_processed": len(memories),
            "memory_links_created": summary["link_count"],
        },
        indent=2,
    )


def query_clusters(
    user_id: str | None = None,
    limit: int = 50,
) -> str:
    """Query cluster entities from the graph store.

    Returns all cluster entities with their member memory count.

    Args:
        user_id: Optional user ID override.
        limit: Maximum number of cluster entities to return (default 50).

    Returns:
        JSON list of cluster entities with member info.
    """
    uid = user_id or USER_ID
    tenant_id = get_current_account_id()
    store = get_graph_store()

    entities = store.get_entities_by_type(uid, "cluster", limit=limit, tenant_id=tenant_id)
    clusters = []
    for entity in entities:
        member_ids = entity.properties.get("member_ids", [])
        clusters.append(
            {
                "cluster_id": entity.id,
                "name": entity.name,
                "description": entity.description,
                "member_count": len(member_ids),
                "member_ids": member_ids[:20],  # truncate for readability
                "properties": entity.properties,
            }
        )

    return json.dumps(
        {
            "ok": True,
            "cluster_count": len(clusters),
            "clusters": clusters,
        },
        indent=2,
    )


# =============================================================================
# Enhanced Synthesis Tools
# =============================================================================


@mcp.tool(output_schema=None)
def link_memories(
    source_memory_id: str,
    target_memory_id: str,
    relationship_type: str,
    user_id: str | None = None,
    confidence: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> str:
    """
    Create or update a typed relationship between two memories.

    Relationship types: 'updates', 'extends', 'derives', 'contradicts',
    'supports', 'related'.

    Args:
        source_memory_id: ID of the source memory (the "from" side).
        target_memory_id: ID of the target memory (the "to" side).
        relationship_type: One of the supported relationship types.
        user_id: Optional user ID override.
        confidence: Confidence score in [0.0, 1.0].
        metadata: Optional JSON-serializable metadata.
    """
    uid = user_id or USER_ID
    store = get_memory_relationship_store()
    try:
        rel = store.link_memories(
            source_memory_id=source_memory_id,
            target_memory_id=target_memory_id,
            relationship_type=relationship_type,
            user_id=uid,
            confidence=confidence,
            metadata=metadata,
        )
    except MemoryRelationshipError as exc:
        return f"Error: {exc}"
    return json.dumps(rel.to_dict(), indent=2)


def get_memory_relationships(
    memory_id: str,
    user_id: str | None = None,
    direction: str = "both",
    relationship_type: str | None = None,
) -> str:
    """
    Return relationships touching a memory.

    Args:
        memory_id: The memory to query.
        user_id: Optional user ID override.
        direction: 'out' (source=memory), 'in' (target=memory), or 'both'.
        relationship_type: Optional filter, e.g. 'updates' or 'extends'.
    """
    uid = user_id or USER_ID
    store = get_memory_relationship_store()
    try:
        rels = store.get_relationships_for_memory(
            memory_id=memory_id,
            user_id=uid,
            direction=direction,
            relationship_type=relationship_type,
        )
    except MemoryRelationshipError as exc:
        return f"Error: {exc}"
    return json.dumps([r.to_dict() for r in rels], indent=2)


def traverse_memory_graph(
    root_memory_id: str,
    user_id: str | None = None,
    max_depth: int = 2,
    limit: int = 100,
) -> str:
    """
    BFS-traverse the memory relationship graph from a root memory.

    Walks edges in both directions up to max_depth and returns the set of
    reachable memory IDs plus the edges traversed.

    Args:
        root_memory_id: Starting memory ID.
        user_id: Optional user ID override.
        max_depth: Maximum traversal depth (0-5).
        limit: Maximum number of nodes to return (1-1000).
    """
    uid = user_id or USER_ID
    store = get_memory_relationship_store()
    try:
        result = store.traverse_memory_graph(
            root_memory_id=root_memory_id,
            user_id=uid,
            max_depth=max_depth,
            limit=limit,
        )
    except MemoryRelationshipError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {
            "root_memory_id": result.root_memory_id,
            "depth": result.depth,
            "nodes": result.nodes,
            "edges": result.edges,
        },
        indent=2,
    )


# =============================================================================
# Semantic Vector Search Tools (MEM-5)
# =============================================================================


def index_memory_embedding(
    memory_id: str,
    text: str,
    user_id: str | None = None,
    provider: str | None = None,
) -> str:
    """
    Compute and store an embedding vector for a memory's text.

    Args:
        memory_id: The memory ID to index.
        text: The text content to embed.
        user_id: Optional user ID override.
        provider: Embedder provider name (default 'local-hash').
    """
    uid = user_id or USER_ID
    prov = provider or _SEMANTIC_DEFAULT_PROVIDER
    try:
        store = get_semantic_search(provider=prov)
        dim = store.index_memory(memory_id=memory_id, text=text, user_id=uid)
    except _SemanticSearchError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {
            "memory_id": memory_id,
            "user_id": uid,
            "provider": prov,
            "dimension": dim,
            "indexed": True,
        },
        indent=2,
    )


def delete_memory_embedding(
    memory_id: str,
    user_id: str | None = None,
    provider: str | None = None,
) -> str:
    """Remove a memory's stored embedding vector."""
    uid = user_id or USER_ID
    prov = provider or _SEMANTIC_DEFAULT_PROVIDER
    try:
        store = get_semantic_search(provider=prov)
        deleted = store.delete_memory_embedding(memory_id=memory_id, user_id=uid)
    except _SemanticSearchError as exc:
        return f"Error: {exc}"
    return json.dumps(
        {
            "memory_id": memory_id,
            "user_id": uid,
            "provider": prov,
            "deleted": deleted,
        },
        indent=2,
    )


def semantic_search_memories(
    query: str,
    user_id: str | None = None,
    limit: int = 10,
    min_score: float = 0.0,
    provider: str | None = None,
) -> str:
    """
    Semantic vector search over stored memory embeddings.

    Args:
        query: Free-text query to embed and match against stored vectors.
        user_id: Optional user ID override.
        limit: Maximum matches to return (1-1000).
        min_score: Minimum cosine similarity threshold in [-1.0, 1.0].
        provider: Embedder provider name (default 'local-hash').
    """
    uid = user_id or USER_ID
    prov = provider or _SEMANTIC_DEFAULT_PROVIDER
    try:
        store = get_semantic_search(provider=prov)

        result = store.search(
            query=query,
            user_id=uid,
            options=SemanticSearchOptions(
                limit=limit,
                min_score=min_score,
            ),
        )
    except _SemanticSearchError as exc:
        return f"Error: {exc}"
    return json.dumps(result.to_dict(), indent=2)


# =============================================================================
# Enhanced Synthesis Tools
# =============================================================================


def _handle_analyze_synthesize(uid: str, tenant_id: str, options: AnalysisAction) -> str:
    """Helper for analyze synthesize action."""
    synthesizer = get_enhanced_synthesizer() if options.enhanced else get_memory_system()["synthesizer"]
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM memories WHERE user_id = ? AND tenant_id = ? AND is_ghost = 0 ORDER BY created_at DESC LIMIT ?",
        (uid, tenant_id, options.limit),
    ).fetchall()
    conn.close()

    if len(rows) < 5:
        return "Need at least 5 memories for synthesis."

    memories = []
    for r in rows:
        emo = json.loads(r["emotional_context"]) if r["emotional_context"] else {}
        memories.append(
            MemoryObject(
                id=r["id"],
                timestamp=r["created_at"],
                scope=r["scope"],
                retention=r["retention"],
                content=r["content"],
                tags=json.loads(r["tags"]),
                emotional_context=EmotionalMetadata(intensity=emo.get("intensity", 0.5)) if emo else None,
            )
        )

    if options.enhanced:
        result = _run_async(synthesizer.synthesize(memories, user_id=uid))
        return json.dumps(result.to_dict(), indent=2) if result else "No results."

    result = _run_async(synthesizer.synthesize(memories))
    if not result:
        return "No results."

    return json.dumps(
        {
            "merged_ids": result.merged_ids,
            "new_memory_id": result.new_memory_id,
            "compression": result.compression_ratio,
            "stance_shifts": len(result.stance_shifts),
        },
        indent=2,
    )


@mcp.tool(output_schema=None)
def analyze_memories(
    options: AnalysisAction | None = None,
    user_id: str | None = None,
    action: Literal["synthesize", "reflect"] | None = None,
    period: str | None = None,
    limit: int | None = None,
    enhanced: bool | None = None,
) -> str:
    """
    Perform analysis on memories: synthesis or reflection.

    Args:
        options: Action and parameters
        user_id: Optional user ID override
        action: Flat parameter for action (optional, fallback if options not provided)
        period: Flat parameter for period (optional)
        limit: Flat parameter for limit (optional)
        enhanced: Flat parameter for enhanced synthesis (optional)
    """
    if options is None:
        if action is None:
            return "Error: either 'options' or 'action' must be provided."
        options = AnalysisAction(
            action=action,
            period=period or "weekly",
            limit=limit if limit is not None else 50,
            enhanced=enhanced if enhanced is not None else False,
        )
    uid = user_id or USER_ID
    tenant_id = get_current_account_id()

    if options.action == "synthesize":
        return _handle_analyze_synthesize(uid, tenant_id, options)

    if options.action == "reflect":
        engine = get_reflection_engine()
        report = engine.reflect(uid, tenant_id=tenant_id, period=options.period)
        if not report:
            return "No results."

        result_dict = report.to_dict()

        # Attempt LLM-powered narrative enhancement (graceful fallback)
        try:
            llm_client = TenantLLMClient.from_env(tenant_id)

            def _llm_call(prompt: str, _tid: str, _uid: str) -> str:
                return llm_client.generate(prompt, user_id=_uid)

            narrative = generate_insight_narrative(
                report,
                tenant_id=tenant_id,
                user_id=uid,
                llm_call=_llm_call,
                model_version=f"{llm_client.provider}/{llm_client.model}",
                cache=_reflection_narrative_cache,
            )
            if narrative:
                result_dict["narrative"] = narrative
        except (LLMNotConfiguredError, LLMError, ReflectionNarrativeError):
            pass  # No LLM configured or call failed — return structured report only

        return json.dumps(result_dict, indent=2)

    return f"Unknown action: {options.action}"


# =============================================================================
# Hybrid Retrieval Tools
# =============================================================================


# =============================================================================
# Reflection Engine Tools
# =============================================================================


# =============================================================================
# Decay Model Tools (MEM-8: Memory Strength Decay Model)
# =============================================================================


def get_memory_strength(
    memory_id: str,
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> str:
    """Read the dynamic strength and trend for a memory.

    Returns the importance (creator-set), the current_strength (decayed),
    the strength_trend, activation_count, last_decay_at, and timestamps.
    The dynamic strength decays over time per the user's
    decay_config; the static importance is preserved.

    Args:
        memory_id: Memory ID to look up.
        user_id: Optional user ID override; defaults to the active user.
        tenant_id: Optional tenant ID override; defaults to the active tenant.
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_account_id()
    result = get_decay_model().get_memory_strength(memory_id=memory_id, user_id=uid, tenant_id=tid)
    if result is None:
        return f"Memory {memory_id} not found for user {uid} in tenant {tid}."
    return json.dumps(result, indent=2)


def apply_memory_decay(
    user_id: str | None = None,
    tenant_id: str | None = None,
    batch_size: int = 500,
) -> str:
    """Run a decay batch for a user's memories.

    Applies the Ebbinghaus-based decay to current_strength for every
    memory in (tenant, user), updates the trend, and records a
    memory_decay_events audit-log row per affected memory.

    Args:
        user_id: Optional user ID override; defaults to the active user.
        tenant_id: Optional tenant ID override; defaults to the active tenant.
        batch_size: Pagination size for the underlying query (default 500).
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_account_id()
    stats = get_decay_model().apply_decay_batch(user_id=uid, tenant_id=tid, batch_size=batch_size)
    return json.dumps(
        {
            "ok": True,
            "action": "apply_memory_decay",
            "user_id": uid,
            "tenant_id": tid,
            **stats.to_dict(),
        },
        indent=2,
    )


def reinforce_memory(
    memory_id: str,
    user_id: str | None = None,
    tenant_id: str | None = None,
    activation_boost: float | None = None,
) -> str:
    """Boost a memory's strength on access.

    Increments activation_count, multiplies current_strength by
    activation_boost (capped at 1.0), updates accessed_at /
    last_retrieved_at / last_decay_at, sets strength_trend, and writes
    a 'reinforce' event to memory_decay_events.

    Args:
        memory_id: Memory ID to reinforce.
        user_id: Optional user ID override.
        tenant_id: Optional tenant ID override.
        activation_boost: Optional override; defaults to the user's
            decay_config.activation_boost.
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_account_id()
    result = get_decay_model().reinforce_memory(
        memory_id=memory_id,
        user_id=uid,
        tenant_id=tid,
        activation_boost=activation_boost,
    )
    if result is None:
        return f"Memory {memory_id} not found for user {uid} in tenant {tid}."
    return json.dumps({"ok": True, "action": "reinforce_memory", **result}, indent=2)


def get_decay_config(
    user_id: str | None = None,
    tenant_id: str | None = None,
    category: str = "general",
) -> str:
    """Return the decay config for a (tenant, user, category) triple.

    Falls back to system defaults when no row exists in decay_config:
    half_life_hours=168, min_importance=0.1, activation_boost=1.2,
    strengthening_threshold=5, stale_threshold=0.2.

    Args:
        user_id: Optional user ID override.
        tenant_id: Optional tenant ID override.
        category: Memory category (default 'general').
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_account_id()
    cfg = get_decay_model().get_decay_config(user_id=uid, tenant_id=tid, category=category)
    return json.dumps({"ok": True, "action": "get_decay_config", **cfg.to_dict()}, indent=2)


def set_decay_config(
    user_id: str | None = None,
    tenant_id: str | None = None,
    category: str = "general",
    half_life_hours: float | None = None,
    min_importance: float | None = None,
    activation_boost: float | None = None,
    strengthening_threshold: int | None = None,
    stale_threshold: float | None = None,
) -> str:
    """Upsert a decay config row for (tenant, user, category).

    None values for individual fields keep whatever the existing row
    holds (or the system default if no row exists). Validation:
    half_life_hours > 0, all thresholds and importance bounds in [0, 1],
    activation_boost in [0, 10].

    Args:
        user_id: Optional user ID override.
        tenant_id: Optional tenant ID override.
        category: Memory category (default 'general').
        half_life_hours: New Ebbinghaus half-life in hours.
        min_importance: Floor for current_strength.
        activation_boost: Multiplier applied on each access.
        strengthening_threshold: Activation count to mark 'strengthening'.
        stale_threshold: Below this strength, trend becomes 'stale'.
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_account_id()
    cfg = get_decay_model().set_decay_config(
        user_id=uid,
        tenant_id=tid,
        category=category,
        options=DecayConfigOptions(
            half_life_hours=half_life_hours,
            min_importance=min_importance,
            activation_boost=activation_boost,
            strengthening_threshold=strengthening_threshold,
            stale_threshold=stale_threshold,
        ),
    )
    return json.dumps({"ok": True, "action": "set_decay_config", **cfg.to_dict()}, indent=2)


def get_decay_events(
    user_id: str | None = None,
    tenant_id: str | None = None,
    memory_id: str | None = None,
    limit: int = 50,
) -> str:
    """Read recent memory_decay_events audit-log rows for a user.

    Each row records a single 'decay' or 'reinforce' event with the
    old/new strength, the decay_factor or boost, a human-readable
    reason, and an ISO timestamp. Useful for compliance review and
    debugging unexpected strength changes.

    Args:
        user_id: Optional user ID override.
        tenant_id: Optional tenant ID override.
        memory_id: Optional filter to a single memory.
        limit: Maximum number of events to return (default 50).
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_account_id()
    events = get_decay_model().get_decay_events(user_id=uid, tenant_id=tid, memory_id=memory_id, limit=limit)
    return json.dumps(
        {
            "ok": True,
            "action": "get_decay_events",
            "count": len(events),
            "events": [e.to_dict() for e in events],
        },
        indent=2,
    )


def run_maintenance(
    options: MaintenanceAction,
    user_id: str | None = None,
    tenant_id: str | None = None,
) -> str:
    """Run a conservative memory maintenance job.

    Performs background memory quality operations: duplicate consolidation,
    contradiction detection, stale archival, and cross-memory synthesis.
    High-impact changes (contradictions) are flagged for admin review and
    never auto-applied.

    Args:
        options: Maintenance configuration (modes, thresholds, budget)
        user_id: Optional user ID override
        tenant_id: Optional tenant ID override
    """
    uid = user_id or USER_ID
    tid = tenant_id or get_current_account_id()

    _check_rate_limit(tid)

    config = MaintenanceConfig(
        tenant_id=tid,
        user_id=uid,
        modes=options.modes,
        duplicate_threshold=options.duplicate_threshold,
        stale_strength_threshold=options.stale_strength_threshold,
        stale_importance_threshold=options.stale_importance_threshold,
        batch_size=options.batch_size,
        max_runtime_seconds=options.max_runtime_seconds,
    )

    job = MemoryMaintenanceJob()
    stats = job.run(config)

    return json.dumps(
        {
            "ok": True,
            "action": "run_maintenance",
            **stats.to_dict(),
        },
        indent=2,
    )


# ============================================================================
# FastMCP 4.0 Native Prompt Templates
# ============================================================================


@mcp.prompt("session_catchup")
def session_catchup_prompt(query: str = "", user_id: str | None = None) -> str:
    """Prepare a comprehensive continuity and memory injection prompt for starting a session."""
    uid = user_id or USER_ID
    snapshot = get_context_snapshot(user_id=uid)
    msg = query if query else "current session context and user goals"
    injected = inject_context(user_message=msg, user_id=uid)

    return f"""# Foresight Continuity & Context Catch-Up

## Active Context Blocks:
{snapshot if snapshot else '(No active context blocks registered)'}

## Retrieved Memories & Directives:
{injected if injected else '(No relevant memories found)'}

## Continuity Instructions:
1. Review the active guidance and relevant facts above before responding to the user.
2. Adhere to all established user preferences and active constraints.
3. Update context blocks or store new memories as significant decisions occur.
"""


@mcp.prompt("curate_review")
def curate_review_prompt(run_id: str | None = None, user_id: str | None = None) -> str:
    """Generate a structured curation review prompt to evaluate pending memory consolidation proposals."""
    uid = user_id or USER_ID
    if run_id:
        run_res = manage_curation_runs(CurationRunAction(action="get", run_id=run_id), user_id=uid)
    else:
        run_res = manage_curation_runs(CurationRunAction(action="list", status="staged"), user_id=uid)

    return f"""# Foresight Curation Review

Evaluate the following proposed memory maintenance and consolidation operations:

```json
{run_res}
```

### Review Objectives:
- Ensure no durable or permanent facts are accidentally degraded or lost.
- Verify that merged memories retain all essential entity tags and attributes.
- Approve or cancel the curation run using `manage_curation_runs`.
"""


@mcp.prompt("user_profile")
def user_profile_prompt(user_id: str | None = None) -> str:
    """Generate a synthesized user profile and behavioral preferences prompt."""
    uid = user_id or USER_ID
    guidance = get_context_whisper(user_id=uid)
    search_res = search_memories(
        options=SearchOptions(query_type="keyword", query="preference habit style communication rule", limit=20),
        user_id=uid,
    )

    return f"""# User Profile & Working Style

## Active Guidance:
{guidance if guidance else '(Standard working mode)'}

## Documented User Preferences & Traits:
{search_res if search_res else '(No specific traits registered)'}
"""


# ============================================================================
# FastMCP 4.0 Resources
# ============================================================================


@mcp.resource("foresight://context-blocks")
def resource_context_blocks() -> str:
    """Live resource stream of all active context blocks."""
    return manage_context_blocks(action="list")


@mcp.resource("foresight://system-status")
def resource_system_status() -> str:
    """Live resource stream of Foresight memory and database health metrics."""
    return get_system_status()


@mcp.resource("foresight://curation-runs")
def resource_curation_runs() -> str:
    """Live resource stream of memory curation runs."""
    return manage_curation_runs(CurationRunAction(action="list"))


# ============================================================================
# FastMCP 4.0 Custom UI Routes & Interactive Dashboard
# ============================================================================

_FORESIGHT_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Foresight — AI Memory & Continuity</title>
  <style>
    :root {
      --bg: #0f1117;
      --panel: #1a1f2e;
      --panel-hover: #23293d;
      --border: #2d3748;
      --primary: #6c9fff;
      --accent: #5eead4;
      --secondary: #c084fc;
      --text: #e2e8f0;
      --text-muted: #94a3b8;
      --success: #4ade80;
      --warning: #fbbf24;
      --error: #f87171;
      --radius: 8px;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 24px;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand h1 {
      font-size: 24px;
      font-weight: 700;
      background: linear-gradient(135deg, var(--primary), var(--accent));
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .badge-live {
      background: rgba(74, 222, 128, 0.15);
      color: var(--success);
      border: 1px solid var(--success);
      padding: 3px 8px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 600;
    }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .stat-card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 16px;
    }
    .stat-label {
      color: var(--text-muted);
      font-size: 13px;
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .stat-value {
      font-size: 26px;
      font-weight: 700;
      color: var(--text);
      margin-top: 6px;
    }
    .tabs {
      display: flex;
      gap: 8px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 20px;
    }
    .tab-btn {
      background: transparent;
      border: none;
      color: var(--text-muted);
      padding: 10px 18px;
      cursor: pointer;
      font-size: 14px;
      font-weight: 600;
      border-bottom: 2px solid transparent;
      transition: all 0.2s;
    }
    .tab-btn.active {
      color: var(--accent);
      border-bottom-color: var(--accent);
    }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    .search-bar {
      display: flex;
      gap: 10px;
      margin-bottom: 20px;
    }
    .search-bar input {
      flex: 1;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 10px 14px;
      color: var(--text);
      font-size: 14px;
      outline: none;
    }
    .search-bar input:focus {
      border-color: var(--primary);
    }
    .btn {
      background: var(--primary);
      color: #fff;
      border: none;
      border-radius: var(--radius);
      padding: 10px 16px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      transition: opacity 0.2s;
    }
    .btn:hover { opacity: 0.9; }
    .card-list {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .item-card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 14px 18px;
      transition: border-color 0.2s, background 0.2s;
    }
    .item-card:hover {
      border-color: var(--primary);
      background: var(--panel-hover);
    }
    .item-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 13px;
    }
    .tag {
      font-size: 11px;
      padding: 2px 8px;
      border-radius: 10px;
      font-weight: 600;
      text-transform: uppercase;
    }
    .tag-fact { background: rgba(108, 159, 255, 0.2); color: var(--primary); }
    .tag-preference { background: rgba(192, 132, 252, 0.2); color: var(--secondary); }
    .tag-insight { background: rgba(94, 234, 212, 0.2); color: var(--accent); }
    .tag-decision { background: rgba(74, 222, 128, 0.2); color: var(--success); }
    .tag-guidance { background: rgba(251, 191, 36, 0.2); color: var(--warning); }
    pre {
      background: #11141d;
      padding: 12px;
      border-radius: 6px;
      font-size: 13px;
      overflow-x: auto;
      color: #94a3b8;
      border: 1px solid #23293d;
    }
  </style>
</head>
<body>
  <div class="header">
    <div class="brand">
      <h1>Foresight</h1>
      <span class="badge-live">FastMCP 4.0</span>
    </div>
    <div style="display:flex;gap:10px;">
      <button class="btn" style="background:#2d3748;" onclick="triggerMaintenance()">Distill Context & Optimize</button>
      <button class="btn" onclick="refreshAll()">Refresh Data</button>
    </div>
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Active Memories</div>
      <div class="stat-value" id="stat-memories">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Database Backend</div>
      <div class="stat-value" id="stat-db" style="font-size: 20px;">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Server Uptime</div>
      <div class="stat-value" id="stat-uptime" style="font-size: 20px;">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Active Tenant</div>
      <div class="stat-value" id="stat-tenant" style="font-size: 20px;">default</div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab-btn active" onclick="switchTab('memories', this)">Memories Explorer</button>
    <button class="tab-btn" onclick="switchTab('blocks', this)">Context Blocks</button>
    <button class="tab-btn" onclick="switchTab('simulator', this)">Context Simulator</button>
    <button class="tab-btn" onclick="switchTab('benchmark', this)">Proof Benchmark</button>
    <button class="tab-btn" onclick="switchTab('raw', this)">System JSON</button>
  </div>

  <div id="tab-memories" class="tab-content active">
    <div class="search-bar">
      <input type="text" id="memory-search" placeholder="Search memories by keyword, entity, or tag..." onkeydown="if(event.key==='Enter') searchMemories()">
      <button class="btn" onclick="searchMemories()">Search</button>
    </div>
    <div id="memories-list" class="card-list">Loading memories...</div>
  </div>

  <div id="tab-blocks" class="tab-content">
    <div id="blocks-list" class="card-list">Loading context blocks...</div>
  </div>

  <div id="tab-simulator" class="tab-content">
    <div class="search-bar">
      <input type="text" id="sim-prompt" placeholder="Simulate user message (e.g. 'What is our tech stack and formatting preferences?')" onkeydown="if(event.key==='Enter') simulateInject()">
      <button class="btn" onclick="simulateInject()">Simulate Auto-Inject</button>
    </div>
    <div id="sim-result">
      <pre style="white-space:pre-wrap;font-family:inherit;background:#11141d;color:var(--text);padding:16px;border-radius:8px;border:1px solid var(--border);">Type a simulated message above to preview the exact context injected into the LLM system prompt.</pre>
    </div>
  </div>

  <div id="tab-benchmark" class="tab-content">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
      <div>
        <h3 style="margin:0 0 4px 0;font-size:18px;">Empirical Production Proof & Continuity Benchmark</h3>
        <p style="margin:0;color:var(--text-muted);font-size:13px;">Demonstrates mathematical proof of cross-session retention, turn self-noting, and multi-surface readiness.</p>
      </div>
      <button class="btn" id="btn-run-bench" onclick="runBenchmark()">Run Proof Benchmark</button>
    </div>
    <div id="benchmark-result">
      <div class="item-card" style="text-align:center;padding:32px;color:var(--text-muted);">
        Click <strong>Run Proof Benchmark</strong> to execute the full evaluation suite across all 8 proof dimensions and multi-surface integrations.
      </div>
    </div>
  </div>

  <div id="tab-raw" class="tab-content">
    <pre id="raw-status">Loading raw system status...</pre>
  </div>

  <script>
    function switchTab(name, btn) {
      document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
      document.querySelectorAll('.tab-btn').forEach(el => el.classList.remove('active'));
      document.getElementById('tab-' + name).classList.add('active');
      btn.classList.add('active');
    }

    async function refreshAll() {
      await Promise.all([loadStatus(), searchMemories(), loadBlocks()]);
    }

    async function triggerMaintenance() {
      const btn = event.target;
      const orig = btn.innerText;
      btn.innerText = 'Optimizing...';
      try {
        const res = await fetch('/ui/api/maintenance', { method: 'POST' });
        const data = await res.json();
        alert('Optimization & context distillation complete! Distilled: ' + (data.distillation?.distilled_blocks?.join(', ') || 'none needed'));
        await refreshAll();
      } catch (e) {
        alert('Optimization error: ' + e);
      } finally {
        btn.innerText = orig;
      }
    }

    async function simulateInject() {
      const text = document.getElementById('sim-prompt').value;
      const resContainer = document.getElementById('sim-result');
      resContainer.innerHTML = '<pre style="padding:16px;border-radius:8px;">Running hybrid retrieval & lane budgeting...</pre>';
      try {
        const res = await fetch('/ui/api/inject', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text })
        });
        const data = await res.json();
        resContainer.innerHTML = `<pre style="white-space:pre-wrap;font-family:inherit;background:#11141d;color:var(--text);padding:16px;border-radius:8px;border:1px solid var(--border);">${data.formatted || '(No context triggered)'}</pre>`;
      } catch (e) {
        resContainer.innerHTML = '<pre style="color:var(--error);padding:16px;">Failed to simulate injection: ' + e + '</pre>';
      }
    }

    async function loadStatus() {
      try {
        const res = await fetch('/ui/api/status');
        const data = await res.json();
        document.getElementById('stat-memories').innerText = data.memory_count ?? '0';
        document.getElementById('stat-db').innerText = data.database ? 'PostgreSQL' : 'Active';
        document.getElementById('stat-uptime').innerText = data.uptime_seconds ? Math.round(data.uptime_seconds) + 's' : 'Online';
        document.getElementById('stat-tenant').innerText = data.tenant_id ?? 'default';
        document.getElementById('raw-status').innerText = JSON.stringify(data, null, 2);
      } catch (e) {
        console.error(e);
      }
    }

    async function searchMemories() {
      const q = document.getElementById('memory-search').value;
      const list = document.getElementById('memories-list');
      list.innerHTML = 'Searching...';
      try {
        const res = await fetch('/ui/api/memories?q=' + encodeURIComponent(q));
        const data = await res.json();
        const items = data.items || [];
        if (items.length === 0) {
          list.innerHTML = '<div class="item-card">No memories found.</div>';
          return;
        }
        list.innerHTML = items.map(m => {
          const cat = m.category || 'fact';
          const content = m.content || '';
          const id = m.id || m.memory_id || '';
          const scope = m.scope || 'session';
          return `
            <div class="item-card">
              <div class="item-header">
                <div><span class="tag tag-${cat}">${cat}</span> <span style="color:var(--text-muted);margin-left:8px;">${id.slice(0, 16)}</span></div>
                <span style="color:var(--text-muted);font-size:12px;">scope: ${scope}</span>
              </div>
              <div style="font-size:14px;color:var(--text);">${content}</div>
            </div>
          `;
        }).join('');
      } catch (e) {
        list.innerHTML = '<div class="item-card" style="color:var(--error);">Failed to load memories: ' + e + '</div>';
      }
    }

    async function loadBlocks() {
      const list = document.getElementById('blocks-list');
      list.innerHTML = 'Loading blocks...';
      try {
        const res = await fetch('/ui/api/context-blocks');
        const data = await res.json();
        const blocks = data.blocks || [];
        if (blocks.length === 0) {
          list.innerHTML = '<div class="item-card">No context blocks found.</div>';
          return;
        }
        list.innerHTML = blocks.map(b => `
          <div class="item-card">
            <div class="item-header">
              <span class="tag tag-guidance">${b.label || 'block'}</span>
              <span style="color:var(--text-muted);font-size:12px;">${b.updated_at || ''}</span>
            </div>
            <pre style="white-space:pre-wrap;font-family:inherit;background:transparent;border:none;padding:0;color:var(--text);font-size:14px;">${b.content || '(empty)'}</pre>
          </div>
        `).join('');
      } catch (e) {
        list.innerHTML = '<div class="item-card" style="color:var(--error);">Failed to load blocks: ' + e + '</div>';
      }
    }

    async function runBenchmark() {
      const resultDiv = document.getElementById('benchmark-result');
      const btn = document.getElementById('btn-run-bench');
      btn.disabled = true;
      btn.textContent = 'Running Benchmark Suite...';
      resultDiv.innerHTML = '<div class="item-card" style="text-align:center;padding:32px;color:var(--primary);">Running 8 real-world proof scenarios across memory retention, turn self-noting, and multi-surface integrations...</div>';
      try {
        const res = await fetch('/ui/api/benchmark', { method: 'POST' });
        const rep = await res.json();
        btn.disabled = false;
        btn.textContent = 'Run Proof Benchmark';
        resultDiv.innerHTML = `
          <div class="stats-grid" style="margin-bottom:20px;">
            <div class="stat-card">
              <div class="stat-label">Composite Score</div>
              <div class="stat-value" style="color:var(--success);">${rep.composite_production_score || 0}/100</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">Scenarios Passed</div>
              <div class="stat-value" style="color:var(--accent);">${rep.passed_scenarios}/${rep.total_scenarios} (${Math.round(rep.success_rate_pct || 0)}%)</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">Foresight Lift</div>
              <div class="stat-value" style="color:var(--primary);">+${Math.round(rep.overall_lift_pct || 0)}%</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">Est. Time Saved</div>
              <div class="stat-value" style="color:var(--secondary);font-size:20px;">~${rep.estimated_hours_saved_monthly || 0} hrs/mo</div>
            </div>
          </div>
          <div class="card-list">
            ${(rep.scenarios || []).map(s => `
              <div class="item-card" style="border-left: 4px solid ${s.passed ? 'var(--success)' : 'var(--error)'}">
                <div class="item-header">
                  <div>
                    <span class="tag ${s.passed ? 'tag-decision' : 'tag-guidance'}">${s.passed ? 'PASS' : 'FAIL'}</span>
                    <strong style="margin-left:8px;font-size:15px;">${s.name}</strong>
                    <span style="color:var(--text-muted);font-size:12px;margin-left:8px;">(${s.dimension})</span>
                  </div>
                  <span style="color:var(--text-muted);font-size:12px;">${s.latency_ms}ms</span>
                </div>
                <div style="font-size:13px;color:var(--text);margin:6px 0;"><strong>Amnesia:</strong> ${Math.round(s.amnesia_score * 100)}% → <strong>Foresight:</strong> <span style="color:var(--success);font-weight:600;">${Math.round(s.foresight_score * 100)}%</span> (${s.improvement_factor}x)</div>
                <div style="font-size:13px;color:var(--accent);">${s.key_takeaway || s.description}</div>
              </div>
            `).join('')}
          </div>
        `;
      } catch (e) {
        btn.disabled = false;
        btn.textContent = 'Run Proof Benchmark';
        resultDiv.innerHTML = '<div class="item-card" style="color:var(--error);">Failed to run benchmark: ' + e + '</div>';
      }
    }

    window.onload = refreshAll;
  </script>
</body>
</html>
"""


@mcp.custom_route("/ui/dashboard", methods=["GET"])
async def ui_dashboard_endpoint(request: Any) -> Any:
    """Serve the modern interactive Foresight MCP UI Dashboard."""
    from starlette.responses import HTMLResponse

    return HTMLResponse(_FORESIGHT_DASHBOARD_HTML)


@mcp.custom_route("/ui/api/status", methods=["GET"])
async def ui_api_status(request: Any) -> Any:
    """API endpoint for UI dashboard stats."""
    from starlette.responses import JSONResponse

    status_raw = get_system_status()
    try:
        data = json.loads(status_raw) if isinstance(status_raw, str) else status_raw
    except Exception:
        data = {"status": "ok", "raw": str(status_raw)}
    return JSONResponse(data)


@mcp.custom_route("/ui/api/memories", methods=["GET"])
async def ui_api_memories(request: Any) -> Any:
    """API endpoint for UI memory search."""
    from starlette.responses import JSONResponse

    q = getattr(request, "query_params", {}).get("q", "")
    limit_val = getattr(request, "query_params", {}).get("limit", "50")
    try:
        limit = int(limit_val)
    except Exception:
        limit = 50

    if q:
        res = search_memories(options=SearchOptions(query_type="keyword", query=q, limit=limit))
    else:
        res = search_memories(options=SearchOptions(query_type="list", limit=limit))

    items = json.loads(res) if isinstance(res, str) else (res or [])
    return JSONResponse({"items": items if isinstance(items, list) else []})


@mcp.custom_route("/ui/api/context-blocks", methods=["GET"])
async def ui_api_context_blocks(request: Any) -> Any:
    """API endpoint for UI context blocks."""
    from starlette.responses import JSONResponse

    res = manage_context_blocks(action="list")
    try:
        data = json.loads(res) if isinstance(res, str) else res
    except Exception:
        data = {"blocks": []}
    return JSONResponse(data if isinstance(data, dict) else {"blocks": []})


@mcp.custom_route("/ui/api/inject", methods=["POST"])
async def ui_api_inject(request: Any) -> Any:
    """API endpoint for testing context injection live in UI."""
    from starlette.responses import JSONResponse

    try:
        body = await request.json()
        text = body.get("text", "")
    except Exception:
        text = ""

    res = inject_context(conversation_text=text, include_details=True)
    try:
        data = json.loads(res) if isinstance(res, str) else res
    except Exception:
        data = {"formatted": str(res)}
    return JSONResponse(data if isinstance(data, dict) else {"formatted": str(res)})


@mcp.custom_route("/ui/api/maintenance", methods=["POST"])
async def ui_api_maintenance(request: Any) -> Any:
    """API endpoint for triggering background maintenance and auto-distillation."""
    from starlette.responses import JSONResponse
    from .context_blocks import auto_distill_context_blocks

    uid = USER_ID
    tid = get_current_account_id()
    config = MaintenanceConfig(tenant_id=tid, user_id=uid)
    job = MemoryMaintenanceJob()
    stats = job.run(config)
    distill = auto_distill_context_blocks(uid, tid)
    return JSONResponse({"ok": True, "maintenance": stats.to_dict(), "distillation": distill})


@mcp.custom_route("/ui/api/benchmark", methods=["GET", "POST"])
async def ui_api_benchmark(request: Any) -> Any:
    """API endpoint for running the Foresight Production Value & Proof Benchmark Suite."""
    from starlette.responses import JSONResponse
    from .proof_benchmark import run_proof_benchmark

    try:
        report = run_proof_benchmark()
        return JSONResponse(report.to_dict())
    except Exception as e:
        logger.exception("Benchmark run failed: %s", e)
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


if __name__ == "__main__":
    import warnings as _w

    _w.filterwarnings("ignore", category=RuntimeWarning, message=".*found in sys.modules.*")

    import sys as _sys

    _this = _sys.modules[__name__]
    _sys.modules["foresight.server"] = _this
    _sys.modules[__name__] = _this
    main()
