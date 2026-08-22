"""
Temporal Schema Migrations for Time-Series Memory Support.

Adds temporal fields to existing memories table for:
- Decay tracking (importance, activation count)
- Freshness trends (stable/strengthening/weakening/stale)
- Time-based queries (created_at, accessed_at)
"""

from __future__ import annotations

from typing import Any

from .connection_pool import get_pool

TEMPORAL_SCHEMA_SQL = """
-- Temporal fields for memory decay and tracking
ALTER TABLE memories ADD COLUMN accessed_at TEXT DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE memories ADD COLUMN importance REAL DEFAULT 1.0;
ALTER TABLE memories ADD COLUMN decay_rate REAL DEFAULT 0.01;
ALTER TABLE memories ADD COLUMN activation_count INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN retrieval_count INTEGER DEFAULT 0;
ALTER TABLE memories ADD COLUMN strength_trend TEXT DEFAULT 'stable'
    CHECK(strength_trend IN ('stable', 'strengthening', 'weakening', 'stale'));
ALTER TABLE memories ADD COLUMN last_retrieved_at TEXT;
ALTER TABLE memories ADD COLUMN category TEXT DEFAULT 'general';

-- Indexes for temporal queries
CREATE INDEX IF NOT EXISTS idx_memories_user_created ON memories(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_user_accessed ON memories(user_id, accessed_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(user_id, importance DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_strength_trend ON memories(user_id, strength_trend, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(user_id, category, created_at DESC);
"""


DECAY_CONFIG_SCHEMA = """
-- Configuration table for decay parameters per user/category
CREATE TABLE IF NOT EXISTS decay_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    half_life_hours REAL DEFAULT 168.0,
    min_importance REAL DEFAULT 0.1,
    activation_boost REAL DEFAULT 1.2,
    strengthening_threshold INTEGER DEFAULT 5,
    stale_threshold REAL DEFAULT 0.2,
    UNIQUE(user_id, category)
);

INSERT INTO decay_config (user_id, category, half_life_hours, min_importance, activation_boost, strengthening_threshold, stale_threshold)
VALUES
    ('default', 'general', 168.0, 0.1, 1.2, 5, 0.2),
    ('default', 'preference', 336.0, 0.1, 1.2, 5, 0.2),
    ('default', 'conversation', 84.0, 0.1, 1.2, 5, 0.2),
    ('default', 'fact', 252.0, 0.1, 1.2, 5, 0.2),
    ('default', 'crisis', 8760.0, 0.1, 1.2, 5, 0.2)
ON CONFLICT (user_id, category) DO NOTHING;
"""


def run_temporal_migrations(db_path: str) -> None:
    """Run temporal schema migrations on existing database."""
    pool = get_pool(db_path)
    conn = pool.acquire()

    try:
        for statement in TEMPORAL_SCHEMA_SQL.split(";"):
            stmt = statement.strip()
            if stmt:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise

        for statement in DECAY_CONFIG_SCHEMA.split(";"):
            stmt = statement.strip()
            if stmt:
                try:
                    conn.execute(stmt)
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        raise

        conn.commit()

    except Exception as e:
        conn.rollback()
        raise RuntimeError("Migration failed: database error") from e
    finally:
        conn.close()


def initialize_decay_config(db_path: str, user_id: str) -> None:
    """Initialize decay configuration for a new user."""
    pool = get_pool(db_path)
    conn = pool.acquire()

    try:
        conn.execute(
            """
            INSERT INTO decay_config
            (user_id, category, half_life_hours, min_importance, activation_boost, strengthening_threshold, stale_threshold)
            SELECT ?, category, half_life_hours, min_importance, activation_boost, strengthening_threshold, stale_threshold
            FROM decay_config WHERE user_id = 'default'
            ON CONFLICT (user_id, category) DO NOTHING
        """,
            (user_id,),
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    from .config import DB_PATH

    assert DB_PATH is not None, "DB_PATH required"
    run_temporal_migrations(DB_PATH)
