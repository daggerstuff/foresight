"""Tests for lightweight Memory GC (PIX-3956).

Covers all four phases: expired retention, decay events pruning,
maintenance events pruning, and orphan cleanup.
"""

import json
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from foresight.memory_gc import (
    DEFAULT_DECAY_EVENTS_RETENTION_DAYS,
    DEFAULT_MAINTENANCE_EVENTS_RETENTION_DAYS,
    DEFAULT_MAX_BATCH_SIZE,
    GCConfig,
    GCStats,
    MemoryGC,
    get_memory_gc,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_db() -> str:
    """Create a temp DB with the schema expected by MemoryGC."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    conn.execute(
        """CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL DEFAULT 'default',
            content TEXT,
            scope TEXT DEFAULT 'session',
            retention TEXT DEFAULT 'short_term',
            category TEXT DEFAULT 'general',
            importance REAL DEFAULT 0.5,
            current_strength REAL DEFAULT 1.0,
            strength_trend TEXT DEFAULT 'stable',
            activation_count INTEGER DEFAULT 0,
            is_ghost INTEGER DEFAULT 0,
            gist TEXT,
            synthesized_from TEXT,
            emotional_context TEXT,
            metrics TEXT,
            tags TEXT,
            source_tool TEXT,
            updated_at TEXT,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            expired_at TEXT,
            expired_by TEXT,
            is_sensitive INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_policies (
            tenant_id TEXT NOT NULL DEFAULT 'default',
            user_id TEXT NOT NULL,
            preset TEXT,
            policy_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_decay_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT,
            user_id TEXT,
            memory_id TEXT,
            event_type TEXT,
            old_strength REAL,
            new_strength REAL,
            decay_factor REAL,
            reason TEXT,
            created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            event_type TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            actor TEXT,
            entity_id TEXT,
            payload TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_entity_links (
            memory_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            tenant_id TEXT,
            user_id TEXT,
            relevance_score REAL DEFAULT 1.0,
            PRIMARY KEY (memory_id, entity_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS memory_embeddings (
            memory_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'local-hash',
            dimension INTEGER DEFAULT 768,
            vector BLOB,
            PRIMARY KEY (tenant_id, user_id, memory_id, provider)
        )"""
    )
    conn.commit()
    conn.close()
    return db_path


@dataclass(frozen=True)
class _MemoryRow:
    """Non-identity columns of one seeded ``memories`` row."""

    retention: str = "short_term"
    tenant_id: str = "t1"
    user_id: str = "u1"
    created_at: str | None = None


def _insert_memory(conn, mid: str, content: str, row: _MemoryRow | None = None) -> None:
    row = row or _MemoryRow()
    conn.execute(
        "INSERT INTO memories (id, user_id, tenant_id, content, scope, retention, importance, strength_trend, "
        "is_ghost, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mid,
            row.user_id,
            row.tenant_id,
            content,
            "session",
            row.retention,
            0.5,
            "stable",
            0,
            row.created_at or datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()


def _insert_decay_event(
    conn,
    tenant_id: str = "t1",
    user_id: str = "u1",
    memory_id: str = "m1",
    created_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO memory_decay_events (tenant_id, user_id, memory_id, event_type, old_strength, "
        "new_strength, decay_factor, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tenant_id,
            user_id,
            memory_id,
            "decay",
            1.0,
            0.8,
            0.8,
            "test decay",
            created_at or datetime.now(UTC).isoformat(),
        ),
    )
    conn.commit()


def _insert_maintenance_event(
    conn,
    event_type: str = "maintenance_review",
    tenant_id: str = "t1",
    timestamp: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO events (id, tenant_id, event_type, timestamp, actor, entity_id, payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            f"evt_{event_type}_{datetime.now(UTC).timestamp()}",
            tenant_id,
            event_type,
            timestamp or datetime.now(UTC).isoformat(),
            "gc_test",
            None,
            None,
        ),
    )
    conn.commit()


def _insert_entity_link(
    conn,
    memory_id: str,
    entity_id: str = "ent1",
    tenant_id: str = "t1",
) -> None:
    conn.execute(
        "INSERT INTO memory_entity_links (memory_id, entity_id, tenant_id, user_id, relevance_score) "
        "VALUES (?, ?, ?, ?, ?)",
        (memory_id, entity_id, tenant_id, "u1", 1.0),
    )
    conn.commit()


def _insert_embedding(
    conn,
    memory_id: str,
    tenant_id: str = "t1",
    user_id: str = "u1",
) -> None:
    conn.execute(
        "INSERT INTO memory_embeddings (memory_id, tenant_id, user_id, provider, dimension, vector) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (memory_id, tenant_id, user_id, "local-hash", 768, b"testvector"),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# GCConfig
# ---------------------------------------------------------------------------


class TestGCConfig:
    def test_default_config(self) -> None:
        cfg = GCConfig()
        assert cfg.decay_events_retention_days == DEFAULT_DECAY_EVENTS_RETENTION_DAYS
        assert cfg.maintenance_events_retention_days == DEFAULT_MAINTENANCE_EVENTS_RETENTION_DAYS
        assert cfg.max_batch_size == DEFAULT_MAX_BATCH_SIZE
        assert cfg.retention_ttls is None

    def test_custom_config(self) -> None:
        retention_days = 7
        cfg = GCConfig(retention_ttls={"ephemeral": 12}, decay_events_retention_days=retention_days)
        assert cfg.retention_ttls == {"ephemeral": 12}
        assert cfg.decay_events_retention_days == retention_days

    def test_to_dict(self) -> None:
        cfg = GCConfig()
        d = cfg.to_dict()
        assert "retention_ttls" in d
        assert "decay_events_retention_days" in d
        assert d["decay_events_retention_days"] == DEFAULT_DECAY_EVENTS_RETENTION_DAYS


# ---------------------------------------------------------------------------
# GCStats
# ---------------------------------------------------------------------------


class TestGCStats:
    def test_to_dict_keys(self) -> None:
        stats = GCStats()
        d = stats.to_dict()
        assert "expired_memories_found" in d
        assert "expired_memories_deleted" in d
        assert "decay_events_pruned" in d
        assert "maintenance_events_pruned" in d
        assert "orphan_links_cleaned" in d
        assert "orphan_embeddings_cleaned" in d
        assert "gc_duration_seconds" in d
        assert "bytes_freed" in d

    def test_defaults_zero(self) -> None:
        stats = GCStats()
        d = stats.to_dict()
        for v in d.values():
            assert v in {0}, f"expected zero, got {v}"


# ---------------------------------------------------------------------------
# MemoryGC - Phase 1: Expired retention cleanup
# ---------------------------------------------------------------------------


class TestPhase1ExpiredRetention:
    def test_deletes_expired_ephemeral(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        _insert_memory(conn, "m1", "old ephemeral", _MemoryRow(retention="ephemeral", created_at=old))
        _insert_memory(conn, "m2", "fresh ephemeral", _MemoryRow(retention="ephemeral"))
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_found == 1
        assert stats.expired_memories_deleted == 1

        conn2 = sqlite3.connect(db_path)
        remaining = conn2.execute("SELECT id FROM memories").fetchall()
        assert [r[0] for r in remaining] == ["m2"]
        conn2.close()

    def test_deletes_expired_short_term(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(days=14)).isoformat()
        _insert_memory(conn, "m1", "old short_term", _MemoryRow(retention="short_term", created_at=old))
        _insert_memory(conn, "m2", "recent short_term", _MemoryRow(retention="short_term"))
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_found == 1
        assert stats.expired_memories_deleted == 1
        assert stats.bytes_freed > 0

    def test_preserves_long_term_and_permanent(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(days=365)).isoformat()
        _insert_memory(conn, "m1", "old long_term", _MemoryRow(retention="long_term", created_at=old))
        _insert_memory(conn, "m2", "old permanent", _MemoryRow(retention="permanent", created_at=old))
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_found == 0
        assert stats.expired_memories_deleted == 0

        conn2 = sqlite3.connect(db_path)
        remaining = conn2.execute("SELECT id FROM memories ORDER BY id").fetchall()
        assert [r[0] for r in remaining] == ["m1", "m2"]
        conn2.close()

    def test_no_expired_memories(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        _insert_memory(conn, "m1", "fresh", _MemoryRow(retention="ephemeral"))
        _insert_memory(conn, "m2", "fresh", _MemoryRow(retention="short_term"))
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_found == 0
        assert stats.expired_memories_deleted == 0

    def test_tenant_isolation(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        _insert_memory(conn, "m1", "t1 old", _MemoryRow(retention="ephemeral", tenant_id="t1", created_at=old))
        _insert_memory(conn, "m2", "t2 old", _MemoryRow(retention="ephemeral", tenant_id="t2", created_at=old))
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_deleted == 1

        conn2 = sqlite3.connect(db_path)
        remaining = conn2.execute("SELECT id FROM memories").fetchall()
        assert [r[0] for r in remaining] == ["m2"]
        conn2.close()

    def test_policy_expired_rows_survive_retention_sweep(self) -> None:
        """A reversibly expired row is restorable, so the TTL sweep must skip it."""
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(days=14)).isoformat()
        _insert_memory(conn, "m1", "stamped", _MemoryRow(retention="short_term", created_at=old))
        conn.execute("UPDATE memories SET expired_at = ? WHERE id = 'm1'", (datetime.now(UTC).isoformat(),))
        conn.commit()
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_found == 0
        assert stats.expired_memories_deleted == 0

        conn2 = sqlite3.connect(db_path)
        assert conn2.execute("SELECT id FROM memories").fetchall() == [("m1",)]
        conn2.close()

    def test_explicit_expiry_deletes_policy_expired_rows(self) -> None:
        """A user-set ``expires_at`` outranks the reversible policy stamp."""
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        _insert_memory(conn, "m1", "explicit", _MemoryRow(retention="long_term"))
        conn.execute(
            "UPDATE memories SET expires_at = ?, expired_at = ? WHERE id = 'm1'",
            (past, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_deleted == 1

        conn2 = sqlite3.connect(db_path)
        assert conn2.execute("SELECT id FROM memories").fetchall() == []
        conn2.close()

    def test_policy_pinned_row_survives_retention_sweep(self) -> None:
        """A rule with expire_after='never' outranks the legacy short-term TTL."""
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(days=14)).isoformat()
        _insert_memory(conn, "pinned", "pinned note", _MemoryRow(retention="short_term", created_at=old))
        _insert_memory(conn, "ttl", "plain note", _MemoryRow(retention="short_term", created_at=old))
        conn.execute("UPDATE memories SET category = 'note' WHERE id = 'pinned'")
        conn.execute(
            "INSERT INTO memory_policies (tenant_id, user_id, preset, policy_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                "t1",
                "u1",
                "custom",
                json.dumps(
                    {
                        "rules": [{"name": "pin-notes", "match": {"category": "note"}, "expire_after": "never"}],
                        "purge_expired_after": "never",
                    }
                ),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        stats = MemoryGC(db_path).run(tenant_id="t1")

        assert stats.expired_memories_deleted == 1
        conn2 = sqlite3.connect(db_path)
        assert conn2.execute("SELECT id FROM memories").fetchall() == [("pinned",)]
        conn2.close()

    def test_explicit_expiry_outranks_policy_pin(self) -> None:
        """An explicit expires_at is the user's own deadline, even for a pinned row."""
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        _insert_memory(conn, "pinned", "pinned note", _MemoryRow(retention="short_term"))
        conn.execute("UPDATE memories SET category = 'note', expires_at = ? WHERE id = 'pinned'", (past,))
        conn.execute(
            "INSERT INTO memory_policies (tenant_id, user_id, preset, policy_json, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                "t1",
                "u1",
                "custom",
                json.dumps(
                    {
                        "rules": [{"name": "pin-notes", "match": {"category": "note"}, "expire_after": "never"}],
                        "purge_expired_after": "never",
                    }
                ),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        conn.close()

        MemoryGC(db_path).run(tenant_id="t1")

        conn2 = sqlite3.connect(db_path)
        assert conn2.execute("SELECT id FROM memories").fetchall() == []
        conn2.close()

    def test_sensitive_rows_survive_legacy_sweeps(self) -> None:
        """The is_sensitive contract forbids automatic archival by TTL."""
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(days=14)).isoformat()
        _insert_memory(conn, "sensitive", "private note", _MemoryRow(retention="short_term", created_at=old))
        conn.execute("UPDATE memories SET is_sensitive = 1 WHERE id = 'sensitive'")
        conn.commit()
        conn.close()

        MemoryGC(db_path).run(tenant_id="t1")

        conn2 = sqlite3.connect(db_path)
        assert conn2.execute("SELECT id FROM memories").fetchall() == [("sensitive",)]
        conn2.close()

    def test_batch_limiting(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        memory_count = 20
        for i in range(memory_count):
            _insert_memory(conn, f"m{i}", f"old {i}", _MemoryRow(retention="ephemeral", created_at=old))
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1", config=GCConfig(max_batch_size=5))

        assert stats.expired_memories_found == memory_count
        assert stats.expired_memories_deleted == memory_count


# ---------------------------------------------------------------------------
# MemoryGC - Phase 1.5: Explicit expires_at cleanup (PIX-4721)
# ---------------------------------------------------------------------------


def _insert_memory_with_expiry(
    conn,
    mid: str,
    content: str,
    expires_at: str | None,
    row: _MemoryRow | None = None,
) -> None:
    row = row or _MemoryRow()
    conn.execute(
        "INSERT INTO memories (id, user_id, tenant_id, content, scope, retention, importance, "
        "strength_trend, is_ghost, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mid,
            row.user_id,
            row.tenant_id,
            content,
            "session",
            "short_term",
            0.5,
            "stable",
            0,
            datetime.now(UTC).isoformat(),
            expires_at,
        ),
    )
    conn.commit()


class TestPhase15ExplicitExpiry:
    def test_deletes_memories_past_expires_at(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        _insert_memory_with_expiry(conn, "m1", "already expired", past)
        _insert_memory_with_expiry(conn, "m2", "not yet expired", future)
        _insert_memory_with_expiry(conn, "m3", "no expiry", None)
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_deleted == 1

        conn2 = sqlite3.connect(db_path)
        remaining = [r[0] for r in conn2.execute("SELECT id FROM memories ORDER BY id").fetchall()]
        assert remaining == ["m2", "m3"]
        conn2.close()

    def test_expires_at_respects_tenant_isolation(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        _insert_memory_with_expiry(conn, "m1", "t1 expired", past, _MemoryRow(tenant_id="t1"))
        _insert_memory_with_expiry(conn, "m2", "t2 expired", past, _MemoryRow(tenant_id="t2"))
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_deleted == 1

        conn2 = sqlite3.connect(db_path)
        remaining = [r[0] for r in conn2.execute("SELECT id FROM memories ORDER BY id").fetchall()]
        assert remaining == ["m2"]
        conn2.close()

    def test_no_explicit_expiry_deletes_nothing(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        future = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        _insert_memory_with_expiry(conn, "m1", "future", future)
        _insert_memory_with_expiry(conn, "m2", "none", None)
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_deleted == 0


# ---------------------------------------------------------------------------
# MemoryGC - Phase 2: Decay events pruning
# ---------------------------------------------------------------------------


class TestPhase2DecayEventsPruning:
    def test_prunes_old_decay_events(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        _insert_memory(conn, "m1", "test")
        old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        _insert_decay_event(conn, memory_id="m1", created_at=old)
        _insert_decay_event(conn, memory_id="m1")  # recent
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.decay_events_pruned == 1

        conn2 = sqlite3.connect(db_path)
        remaining = conn2.execute("SELECT COUNT(*) FROM memory_decay_events").fetchone()[0]
        assert remaining == 1
        conn2.close()

    def test_no_old_decay_events(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        _insert_memory(conn, "m1", "test")
        _insert_decay_event(conn, memory_id="m1")  # now
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.decay_events_pruned == 0


# ---------------------------------------------------------------------------
# MemoryGC - Phase 3: Maintenance events pruning
# ---------------------------------------------------------------------------


class TestPhase3MaintenanceEvents:
    def test_prunes_old_maintenance_events(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
        _insert_maintenance_event(conn, event_type="maintenance_review", timestamp=old)
        _insert_maintenance_event(conn, event_type="maintenance_insight")  # recent
        _insert_maintenance_event(conn, event_type="maintenance_review:consolidate", timestamp=old)
        conn.close()
        stale_event_count = 2

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.maintenance_events_pruned == stale_event_count

    def test_preserves_non_maintenance_events(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
        _insert_maintenance_event(conn, event_type="memory_access", timestamp=old)
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.maintenance_events_pruned == 0


# ---------------------------------------------------------------------------
# MemoryGC - Phase 4: Orphan cleanup
# ---------------------------------------------------------------------------


class TestPhase4OrphanCleanup:
    def test_cleans_orphaned_entity_links(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        _insert_memory(conn, "m1", "alive")
        _insert_entity_link(conn, memory_id="m1")  # valid link
        _insert_entity_link(conn, memory_id="m_orphan")  # orphan link
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.orphan_links_cleaned == 1

        conn2 = sqlite3.connect(db_path)
        remaining = conn2.execute("SELECT COUNT(*) FROM memory_entity_links").fetchone()[0]
        assert remaining == 1
        conn2.close()

    def test_cleans_orphaned_embeddings(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        _insert_memory(conn, "m1", "alive")
        _insert_embedding(conn, memory_id="m1")  # valid
        _insert_embedding(conn, memory_id="m_orphan")  # orphan
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.orphan_embeddings_cleaned == 1

        conn2 = sqlite3.connect(db_path)
        remaining = conn2.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0]
        assert remaining == 1
        conn2.close()

    def test_no_orphans(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        _insert_memory(conn, "m1", "alive")
        _insert_entity_link(conn, memory_id="m1")
        _insert_embedding(conn, memory_id="m1")
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.orphan_links_cleaned == 0
        assert stats.orphan_embeddings_cleaned == 0


# ---------------------------------------------------------------------------
# Combined run
# ---------------------------------------------------------------------------


class TestCombinedRun:
    def test_all_phases_together(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)

        # Phase 1: expired ephemeral
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        _insert_memory(conn, "m1", "old ephemeral", _MemoryRow(retention="ephemeral", created_at=old))
        _insert_memory(conn, "m2", "recent", _MemoryRow(retention="short_term"))

        # Phase 2: old decay event
        old_decay = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        _insert_decay_event(conn, memory_id="m1", created_at=old_decay)

        # Phase 3: old maintenance event (>60d ago)
        very_old = (datetime.now(UTC) - timedelta(days=90)).isoformat()
        _insert_maintenance_event(conn, event_type="maintenance_review", timestamp=very_old)

        # Phase 4: orphan
        _insert_entity_link(conn, memory_id="m_orphan")

        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")

        assert stats.expired_memories_deleted == 1
        assert stats.decay_events_pruned == 1
        assert stats.maintenance_events_pruned == 1
        assert stats.orphan_links_cleaned == 1
        assert stats.gc_duration_seconds > 0


# ---------------------------------------------------------------------------
# Factory / singleton
# ---------------------------------------------------------------------------


class TestFactory:
    def test_get_memory_gc(self) -> None:
        gc = get_memory_gc("/tmp/test_gc.db")
        assert isinstance(gc, MemoryGC)
        assert gc.db_path == "/tmp/test_gc.db"

    def test_run_memory_gc(self) -> None:
        db_path = _make_test_db()
        conn = sqlite3.connect(db_path)
        old = (datetime.now(UTC) - timedelta(hours=48)).isoformat()
        _insert_memory(conn, "m1", "old too", _MemoryRow(retention="ephemeral", created_at=old))
        conn.close()

        gc = MemoryGC(db_path)
        stats = gc.run(tenant_id="t1")
        assert isinstance(stats, GCStats)
        assert stats.expired_memories_found == 1
