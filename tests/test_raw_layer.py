"""Tests for Phase 2 — MemLake append-only raw layer, provenance, reprocess, tombstone."""

from datetime import datetime, timedelta, timezone

import pytest

import foresight.capture as capture_module
from foresight.capture import get_capture_pipeline, reset_capture_pipeline
from foresight.connection_pool import get_pool, reset_pool
from foresight.memory_gc import GCConfig, MemoryGC, tombstone_raw_events


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate DB per test and reset the capture pipeline singleton."""
    db_file = tmp_path / "test_raw_layer.db"
    monkeypatch.setenv("FORESIGHT_DB_PATH", str(db_file))

    import foresight.config as config_module
    import foresight.connection_pool as conn_pool_module
    import foresight.server as server_module
    import foresight.subconscious as subconscious_module
    from foresight.server import init_db
    from foresight.tenant_context import set_current_account_id, set_current_user_id
    from tests._sqlite_backend import SqliteBackend

    monkeypatch.setattr(config_module, "DB_PATH", str(db_file))
    monkeypatch.setattr(conn_pool_module, "DB_PATH", str(db_file))
    monkeypatch.setattr(server_module, "DB_PATH", str(db_file))
    monkeypatch.setattr(subconscious_module, "DB_PATH", str(db_file))
    reset_pool()

    set_current_user_id("_test_user_")
    set_current_account_id("_test_")

    backend = SqliteBackend(db_path=str(db_file))
    init_db(backend=backend)
    reset_capture_pipeline()
    pipeline = get_capture_pipeline()
    pipeline.db_path = str(db_file)

    # Raw retention is OFF by default; tests that need it opt-in via raw_retention_enabled.
    monkeypatch.setattr(capture_module, "RAW_RETENTION", False)

    yield
    reset_pool()
    from foresight.tenant_context import reset_tenant_context

    reset_tenant_context()


@pytest.fixture
def raw_retention_enabled(monkeypatch):
    """Enable the opt-in append-only raw layer for the duration of a test."""
    monkeypatch.setattr(capture_module, "RAW_RETENTION", True)


# ====== Shared helpers ======

_LATENCY_DECISIONS = [
    {"role": "user", "content": "Let's use PostgreSQL for the database backend running on AWS RDS."},
    {"role": "assistant", "content": "Good choice, I'll configure the connection pool with SSL and autocommit."},
    {"role": "user", "content": "We should also use Redis for caching to improve the response times."},
]


def _count_raw_events(db_path, tenant_id=None, user_id=None, session_id=None, tombstone=None):
    clauses = []
    params = []
    if tenant_id is not None:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    if session_id is not None:
        clauses.append("session_id = ?")
        params.append(session_id)
    if tombstone is not None:
        clauses.append("tombstone = ?")
        params.append(int(tombstone))

    sql = "SELECT COUNT(*) FROM raw_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        return conn.execute(sql, tuple(params)).fetchone()[0]
    finally:
        pool.release(conn)


def _get_raw_event_rows(db_path, **filters):
    clauses = []
    params = []
    for key, value in filters.items():
        clauses.append(f"{key} = ?")
        params.append(value)

    sql = "SELECT * FROM raw_events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)

    pool = get_pool(db_path)
    conn = pool.acquire()
    try:
        return conn.execute(sql, tuple(params)).fetchall()
    finally:
        pool.release(conn)


# ====== Raw retention gate ======


class TestRawRetention:
    def test_raw_retention_off_by_default(self):
        assert capture_module.RAW_RETENTION is False

    def test_capture_writes_no_raw_events_when_retention_disabled(self):
        pipeline = get_capture_pipeline()
        pipeline.run("sess_off", _LATENCY_DECISIONS, "_test_user_", tenant_id="_test_")
        assert _count_raw_events(pipeline.db_path, tenant_id="_test_", user_id="_test_user_") == 0


# ====== Capture persistence and provenance ======


class TestCapturePersistence:
    def test_capture_persists_raw_events_when_retention_enabled(self, raw_retention_enabled):  # noqa: ARG002
        pipeline = get_capture_pipeline()
        assert pipeline.db_path is not None
        stats = pipeline.run("sess_on", _LATENCY_DECISIONS, "_test_user_", tenant_id="_test_")
        assert stats.skipped is False

        assert _count_raw_events(pipeline.db_path, tenant_id="_test_", user_id="_test_user_") == len(_LATENCY_DECISIONS)

    def test_provenance_roundtrip(self, raw_retention_enabled):  # noqa: ARG002
        pipeline = get_capture_pipeline()
        msgs = [
            {"role": "user", "content": "I prefer to write unit tests before implementing new features."},
            {"role": "assistant", "content": "That is a solid practice for maintainable code."},
            {"role": "user", "content": "Testing first helps me catch regressions early."},
        ]
        pipeline.run("sess_prov", msgs, "_test_user_", tenant_id="_test_", infer=False)

        pool = get_pool(pipeline.db_path)
        conn = pool.acquire()
        try:
            rows = conn.execute(
                """
                SELECT p.memory_id, r.content, r.role, r.session_id
                FROM memory_provenance p
                JOIN raw_events r ON p.raw_event_id = r.id
                WHERE p.tenant_id = ?
                """,
                ("_test_",),
            ).fetchall()
        finally:
            pool.release(conn)

        assert len(rows) == len(msgs)
        contents = {row["content"] for row in rows}
        for msg in msgs:
            assert msg["content"] in contents

    def test_reprocess_is_idempotent(self, raw_retention_enabled):  # noqa: ARG002
        pipeline = get_capture_pipeline()
        sess_id = "sess_reprocess"
        stats1 = pipeline.run(sess_id, _LATENCY_DECISIONS, "_test_user_", tenant_id="_test_")
        assert stats1.stored >= 1

        initial_raw = _count_raw_events(pipeline.db_path, tenant_id="_test_", user_id="_test_user_")
        initial_memories = self._count_memories(pipeline.db_path, tenant_id="_test_")
        assert initial_raw == len(_LATENCY_DECISIONS)

        re_stats = pipeline.reprocess("_test_user_", tenant_id="_test_")
        assert re_stats.memories_stored == 0
        assert re_stats.duplicates >= 1

        assert _count_raw_events(pipeline.db_path, tenant_id="_test_", user_id="_test_user_") == initial_raw
        assert self._count_memories(pipeline.db_path, tenant_id="_test_") == initial_memories

    def test_tenant_isolation(self, raw_retention_enabled):  # noqa: ARG002
        pipeline = get_capture_pipeline()
        msgs = [
            {"role": "user", "content": "Tenant A keeps raw events isolated from others."},
            {"role": "assistant", "content": "Agreed."},
            {"role": "user", "content": "No other tenant should see this transcript."},
        ]
        pipeline.run("sess_tenant_a", msgs, "user_a", tenant_id="tenant_a", infer=False)
        pipeline.run("sess_tenant_b", msgs, "user_b", tenant_id="tenant_b", infer=False)

        assert _count_raw_events(pipeline.db_path, tenant_id="tenant_a") == len(msgs)
        assert _count_raw_events(pipeline.db_path, tenant_id="tenant_b") == len(msgs)
        assert _count_raw_events(pipeline.db_path, tenant_id="third_party") == 0

        # Cross-tenant memory_provenance isolation too.
        pool = get_pool(pipeline.db_path)
        conn = pool.acquire()
        try:
            for tenant in ("tenant_a", "tenant_b"):
                count = conn.execute(
                    "SELECT COUNT(*) FROM memory_provenance WHERE tenant_id = ?", (tenant,)
                ).fetchone()[0]
                assert count == len(msgs)
        finally:
            pool.release(conn)

    @staticmethod
    def _count_memories(db_path, tenant_id):
        pool = get_pool(db_path)
        conn = pool.acquire()
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()[0]
        finally:
            pool.release(conn)


# ====== Tombstone erasure and retention sweep ======


class TestTombstone:
    def test_tombstone_raw_events_nulls_content_but_keeps_row_shape(self, raw_retention_enabled):  # noqa: ARG002
        pipeline = get_capture_pipeline()
        msgs = [
            {"role": "user", "content": "This sensitive message should be erasable."},
            {"role": "assistant", "content": "Understood."},
        ]
        pipeline.run("sess_erase", msgs, "_test_user_", tenant_id="_test_", infer=False)

        assert _count_raw_events(pipeline.db_path, tenant_id="_test_", session_id="sess_erase") == len(msgs)

        erased = tombstone_raw_events(session_id="sess_erase", tenant_id="_test_", db_path=pipeline.db_path)
        assert erased == len(msgs)

        rows = _get_raw_event_rows(pipeline.db_path, tenant_id="_test_", session_id="sess_erase")
        assert len(rows) == len(msgs)
        for row in rows:
            assert row["tombstone"] == 1
            assert row["content"] is None

        # Provenance links remain (the derived memory was not deleted).
        pool = get_pool(pipeline.db_path)
        conn = pool.acquire()
        try:
            provenance_count = conn.execute(
                "SELECT COUNT(*) FROM memory_provenance WHERE tenant_id = ?", ("_test_",)
            ).fetchone()[0]
            assert provenance_count == len(msgs)
        finally:
            pool.release(conn)

    def test_gc_tombstones_raw_events_past_retention_window(self, raw_retention_enabled):  # noqa: ARG002
        pipeline = get_capture_pipeline()
        tenant_id = "_test_"
        user_id = "_test_user_"

        # Simulate raw events captured three days ago and three minutes ago.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()

        pool = get_pool(pipeline.db_path)
        conn = pool.acquire()
        try:
            conn.execute(
                """
                INSERT INTO raw_events
                (id, tenant_id, user_id, session_id, role, content, position, captured_at, source, metadata, tombstone)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "raw_old", tenant_id, user_id, "sess_gc", "user", "old content", 0, old_ts, "test", "{}", 0,
                    "raw_recent", tenant_id, user_id, "sess_gc", "user", "recent content", 1, recent_ts, "test", "{}", 0,
                ),
            )
            conn.commit()
        finally:
            pool.release(conn)

        gc = MemoryGC(pipeline.db_path)
        stats = gc.run(tenant_id=tenant_id, config=GCConfig(raw_retention_window_days=1))
        assert stats.raw_events_tombstoned == 1

        rows_by_id = {
            row["id"]: row
            for row in _get_raw_event_rows(pipeline.db_path, tenant_id=tenant_id, session_id="sess_gc")
        }
        assert len(rows_by_id) == 2
        assert rows_by_id["raw_old"]["tombstone"] == 1
        assert rows_by_id["raw_old"]["content"] is None
        assert rows_by_id["raw_recent"]["tombstone"] == 0
        assert rows_by_id["raw_recent"]["content"] == "recent content"

    def test_tombstone_requires_at_least_one_filter(self):
        with pytest.raises(ValueError, match="At least one of user_id, session_id, or older_than"):
            tombstone_raw_events(tenant_id="_test_")
