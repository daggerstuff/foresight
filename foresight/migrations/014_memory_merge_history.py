"""
Migration 014 — Memory Merge History table.

Creates the `memory_merge_history` table that records every auto-consolidation
event: which memories were merged, their pre-merge content, the combined
content, and when/by-whom the merge happened. This provides an audit trail
for memory consolidation that was previously lost (ghosted memories had
their content overwritten with no history).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from foresight.backend.base import DatabaseBackend


def migrate(backend: DatabaseBackend) -> None:
    """Create the memory_merge_history table if it does not exist."""
    backend.execute(
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
        )"""
    )
    backend.execute("CREATE INDEX IF NOT EXISTS idx_merge_history_primary ON memory_merge_history(primary_id)")
    backend.execute("CREATE INDEX IF NOT EXISTS idx_merge_history_tenant ON memory_merge_history(tenant_id)")
    backend.execute("CREATE INDEX IF NOT EXISTS idx_merge_history_user ON memory_merge_history(user_id)")
    backend.execute("CREATE INDEX IF NOT EXISTS idx_merge_history_merged_at ON memory_merge_history(merged_at)")
