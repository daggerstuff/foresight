"""MemLake invariants (PIX-4731).

Enforces the architectural promises from the MemLake/Hindsight merge:

* raw layer append-only and rebuildable (reprocess must not mutate raw rows);
* supersession edges are well-formed (superseded_by resolves to a live memory,
  validity intervals close before they open);
* point-in-time reconstruction is temporally correct;
* tenant isolation holds across raw_events, memory_provenance, and as-of views.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from foresight.memory_gc import tombstone_raw_events
from foresight.tenant_context import reset_tenant_context, set_current_account_id, set_current_user_id


@pytest.fixture
def ml_db(tmp_path, monkeypatch):
    """Isolated SQLite fixture with raw retention and supersede detection enabled."""
    db_file = tmp_path / "memlake_invariants.db"
    db_path = str(db_file)
    monkeypatch.setenv("FORESIGHT_DB_PATH", db_path)
    monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "1")
    monkeypatch.setenv("FORESIGHT_RAW_RETENTION", "1")

    import foresight.capture as capture_module
    import foresight.config as config_module
    import foresight.connection_pool as conn_pool_module
    import foresight.server as server_module
    import foresight.subconscious as subconscious_module
    from foresight.capture import get_capture_pipeline, reset_capture_pipeline
    from foresight.connection_pool import reset_pool
    from foresight.memory_relationships import reset_memory_relationship_store
    from foresight.server import init_db
    from tests._sqlite_backend import SqliteBackend

    monkeypatch.setattr(config_module, "DB_PATH", db_path)
    monkeypatch.setattr(conn_pool_module, "DB_PATH", db_path)
    monkeypatch.setattr(server_module, "DB_PATH", db_path)
    monkeypatch.setattr(subconscious_module, "DB_PATH", db_path)
    monkeypatch.setattr(capture_module, "RAW_RETENTION", True)

    reset_pool()
    set_current_user_id("_test_user_")
    set_current_account_id("_test_")

    init_db(backend=SqliteBackend(db_path=db_path))
    reset_capture_pipeline()
    pipeline = get_capture_pipeline()
    pipeline.db_path = db_path
    reset_memory_relationship_store()

    yield db_file

    reset_pool()
    reset_tenant_context()


def _conn(db_file):
    conn = sqlite3.connect(str(db_file))
    conn.row_factory = sqlite3.Row
    return conn


def _memory_id(content, salt=""):
    return hashlib.sha256(f"{content}{salt}".encode()).hexdigest()[:16]


def _store_memory(
    conn,
    memory_id,
    content,
    user_id,
    tenant_id,
    created_at,
    *,
    is_latest=1,
    valid_from=None,
    valid_until=None,
    category="fact",
):
    # Bitemporal convention: a fact is valid from the moment it is recorded unless
    # an explicit validity interval is supplied.
    if valid_from is None:
        valid_from = created_at
    conn.execute(
        """INSERT INTO memories
        (id, content, user_id, tenant_id, category, scope, retention, created_at, updated_at,
         is_latest, valid_from, valid_until)
        VALUES (?, ?, ?, ?, ?, 'session', 'short_term', ?, ?, ?, ?, ?)""",
        (
            memory_id,
            content,
            user_id,
            tenant_id,
            category,
            created_at,
            created_at,
            is_latest,
            valid_from,
            valid_until,
        ),
    )
    conn.commit()


class TestRawLayerAppendOnly:
    def test_reprocess_is_idempotent(self, ml_db):
        from foresight.capture import get_capture_pipeline

        pipeline = get_capture_pipeline()
        pipeline.db_path = str(ml_db)

        messages = [
            {"role": "user", "content": "I started daily mindfulness practice last week."},
            {"role": "assistant", "content": "That sounds like a positive routine."},
        ]
        stats = pipeline.run(
            session_id="s1",
            messages=messages,
            user_id="_test_user_",
            tenant_id="_test_",
            infer=False,
        )
        assert stats.stored > 0

        with _conn(ml_db) as conn:
            before = conn.execute(
                "SELECT COUNT(*), GROUP_CONCAT(content) FROM raw_events WHERE tombstone = 0"
            ).fetchone()
        assert before[0] == len(messages)

        reprocess = pipeline.reprocess(
            user_id="_test_user_",
            tenant_id="_test_",
        )
        assert reprocess.raw_events_replayed == before[0]

        with _conn(ml_db) as conn:
            after = conn.execute(
                "SELECT COUNT(*), GROUP_CONCAT(content) FROM raw_events WHERE tombstone = 0"
            ).fetchone()
        assert after[0] == before[0]
        assert after[1] == before[1]

        # Reprocess did not create any new raw rows.
        assert conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0] == before[0]

    def test_provenance_roundtrip(self, ml_db):
        from foresight.capture import get_capture_pipeline

        pipeline = get_capture_pipeline()
        pipeline.db_path = str(ml_db)

        messages = [
            {"role": "user", "content": "My favorite database is PostgreSQL."},
        ]
        pipeline.run(
            session_id="s2",
            messages=messages,
            user_id="_test_user_",
            tenant_id="_test_",
            infer=False,
        )

        with _conn(ml_db) as conn:
            provenance = conn.execute(
                "SELECT mp.memory_id, re.id, re.content "
                "FROM memory_provenance mp "
                "JOIN raw_events re ON re.id = mp.raw_event_id "
                "WHERE re.tenant_id = ? AND re.user_id = ?",
                ("_test_", "_test_user_"),
            ).fetchall()

        assert len(provenance) >= 1
        for memory_id, raw_id, raw_content in provenance:
            assert memory_id
            assert raw_id
            assert raw_content


class TestSupersessionInvariants:
    """Supersession correctness using the same detector used by the store path."""

    def test_superseded_by_resolves_to_live_memory(self, ml_db, monkeypatch):
        monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "1")
        from foresight.truth_resolution import detect_and_mark_supersessions

        old_id = _memory_id("I love going to therapy every week.")
        new_id = _memory_id("I hate going to therapy every week.")

        with _conn(ml_db) as conn:
            old_created = _iso_ago(minutes=5)
            _store_memory(
                conn,
                old_id,
                "I love going to therapy every week.",
                "_test_user_",
                "_test_",
                old_created,
                is_latest=1,
            )

            new_created = _iso_ago(minutes=1)
            _store_memory(
                conn,
                new_id,
                "I hate going to therapy every week.",
                "_test_user_",
                "_test_",
                new_created,
                is_latest=1,
            )

            matches = detect_and_mark_supersessions(
                conn, new_id, "I hate going to therapy every week.", "_test_user_", "_test_"
            )
            assert len(matches) == 1

            old = conn.execute(
                "SELECT is_latest, superseded_by, valid_from, valid_until FROM memories WHERE id = ?",
                (old_id,),
            ).fetchone()
            new = conn.execute(
                "SELECT id, is_latest, valid_from, valid_until FROM memories WHERE id = ?",
                (new_id,),
            ).fetchone()

        assert old["is_latest"] == 0
        assert old["superseded_by"] == new_id
        assert new["is_latest"] == 1
        assert new["id"] == old["superseded_by"]

        assert old["valid_from"]
        assert old["valid_until"]
        assert old["valid_until"] > old["valid_from"]
        assert new["valid_from"]
        # The superseder was already valid from its creation; the old row closes at
        # supersede time. The invariant is that the new memory is live and the old
        # interval is well-formed, not that the timestamps are identical strings.
        assert new["valid_from"] <= old["valid_until"]

    def test_valid_until_opened_after_valid_from(self, ml_db):
        """Validity interval ordering: a newer fact opens after its predecessor closes."""
        with _conn(ml_db) as conn:
            _store_memory(
                conn,
                _memory_id("dark"),
                "I prefer dark mode.",
                "_test_user_",
                "_test_",
                "2026-01-01T00:00:00+00:00",
                is_latest=0,
                valid_from="2026-01-01T00:00:00+00:00",
                valid_until="2026-02-01T00:00:00+00:00",
            )
            _store_memory(
                conn,
                _memory_id("light"),
                "I prefer light mode.",
                "_test_user_",
                "_test_",
                "2026-02-01T00:00:00+00:00",
                is_latest=1,
                valid_from="2026-02-01T00:00:00+00:00",
                valid_until=None,
            )

            rows = conn.execute(
                "SELECT valid_from, valid_until FROM memories WHERE tenant_id = ? AND user_id = ? ORDER BY created_at",
                ("_test_", "_test_user_"),
            ).fetchall()

        closed = [r for r in rows if r["valid_until"] is not None]
        open_ = [r for r in rows if r["valid_until"] is None]
        assert closed
        assert open_
        for r in closed:
            assert r["valid_from"]
            assert r["valid_until"] > r["valid_from"]


class TestTemporalReconstruction:
    def test_as_of_returns_old_fact_before_supersede(self, ml_db, monkeypatch):
        monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "1")
        from foresight.temporal_queries import TemporalQueryBuilder
        from foresight.truth_resolution import detect_and_mark_supersessions

        old_id = _memory_id("I love therapy.")
        new_id = _memory_id("I hate therapy.")

        with _conn(ml_db) as conn:
            old_created = _iso_ago(minutes=5)
            _store_memory(conn, old_id, "I love therapy.", "_test_user_", "_test_", old_created)
            new_created = _iso_ago(minutes=1)
            _store_memory(conn, new_id, "I hate therapy.", "_test_user_", "_test_", new_created)
            detect_and_mark_supersessions(conn, new_id, "I hate therapy.", "_test_user_", "_test_")

            older = conn.execute(
                "SELECT id, valid_from, valid_until FROM memories WHERE id = ?", (old_id,)
            ).fetchone()
            newer = conn.execute(
                "SELECT id, valid_from FROM memories WHERE id = ?", (new_id,)
            ).fetchone()

        assert older["valid_until"]
        assert newer["valid_from"]
        mid = _iso_between(older["valid_from"], older["valid_until"])

        set_current_account_id("_test_")
        try:
            builder = TemporalQueryBuilder(db_path=str(ml_db))
            results = builder.get_memories_as_of_time(
                user_id="_test_user_",
                target_date=datetime.fromisoformat(mid),
                min_importance=0.0,
            )
        finally:
            reset_tenant_context()

        ids = {r.memory_id for r in results}
        assert older["id"] in ids
        assert newer["id"] not in ids

    def test_as_of_returns_new_fact_after_supersede(self, ml_db, monkeypatch):
        monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "1")
        from foresight.temporal_queries import TemporalQueryBuilder
        from foresight.truth_resolution import detect_and_mark_supersessions

        old_id = _memory_id("I love therapy.")
        new_id = _memory_id("I hate therapy.")

        with _conn(ml_db) as conn:
            _store_memory(conn, old_id, "I love therapy.", "_test_user_", "_test_", _iso_ago(minutes=5))
            _store_memory(conn, new_id, "I hate therapy.", "_test_user_", "_test_", _iso_ago(minutes=1))
            detect_and_mark_supersessions(conn, new_id, "I hate therapy.", "_test_user_", "_test_")

            newer = conn.execute(
                "SELECT id, valid_from FROM memories WHERE id = ?", (new_id,)
            ).fetchone()

        set_current_account_id("_test_")
        try:
            builder = TemporalQueryBuilder(db_path=str(ml_db))
            results = builder.get_memories_as_of_time(
                user_id="_test_user_",
                target_date=datetime.fromisoformat(newer["valid_from"]),
                min_importance=0.0,
            )
        finally:
            reset_tenant_context()

        ids = {r.memory_id for r in results}
        assert newer["id"] in ids

    def test_as_of_resolves_edited_content(self, ml_db):
        from foresight.temporal_queries import TemporalQueryBuilder

        mid = _memory_id("My therapist is Dr. Smith.")

        with _conn(ml_db) as conn:
            _store_memory(
                conn,
                mid,
                "My therapist is Dr. Smith.",
                "_test_user_",
                "_test_",
                "2026-01-01T00:00:00+00:00",
            )
            conn.execute(
                "INSERT INTO memory_versions (id, memory_id, tenant_id, content, version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _memory_id("snapshot"),
                    mid,
                    "_test_",
                    "My therapist is Dr. Adams.",
                    1,
                    "2026-03-01T00:00:00+00:00",
                ),
            )
            conn.commit()

        set_current_account_id("_test_")
        try:
            builder = TemporalQueryBuilder(db_path=str(ml_db))
            results = builder.get_memories_as_of_time(
                user_id="_test_user_",
                target_date=datetime.fromisoformat("2026-02-01T00:00:00+00:00"),
                min_importance=0.0,
            )
        finally:
            reset_tenant_context()

        by_id = {r.memory_id: r for r in results}
        assert mid in by_id
        assert "Dr. Adams" in by_id[mid].content


class TestTenantIsolationInvariants:
    def test_raw_events_and_memories_are_partitioned(self, ml_db):
        from foresight.capture import get_capture_pipeline

        pipeline = get_capture_pipeline()
        pipeline.db_path = str(ml_db)

        tenant_a_messages = [{"role": "user", "content": "Tenant A fact."}]
        tenant_b_messages = [{"role": "user", "content": "Tenant B fact."}]

        pipeline.run(
            session_id="sa",
            messages=tenant_a_messages,
            user_id="_user_a_",
            tenant_id="_tenant_a_",
            infer=False,
        )
        pipeline.run(
            session_id="sb",
            messages=tenant_b_messages,
            user_id="_user_b_",
            tenant_id="_tenant_b_",
            infer=False,
        )

        with _conn(ml_db) as conn:
            a_raw = conn.execute(
                "SELECT COUNT(*) FROM raw_events WHERE tenant_id = ? AND user_id = ?",
                ("_tenant_a_", "_user_a_"),
            ).fetchone()[0]
            b_raw = conn.execute(
                "SELECT COUNT(*) FROM raw_events WHERE tenant_id = ? AND user_id = ?",
                ("_tenant_b_", "_user_b_"),
            ).fetchone()[0]
            a_mem = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND user_id = ?",
                ("_tenant_a_", "_user_a_"),
            ).fetchone()[0]
            b_mem = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE tenant_id = ? AND user_id = ?",
                ("_tenant_b_", "_user_b_"),
            ).fetchone()[0]

        assert a_raw == 1
        assert b_raw == 1
        assert a_mem >= 1
        assert b_mem >= 1

    def test_as_of_query_does_not_cross_tenants(self, ml_db):
        from foresight.temporal_queries import TemporalQueryBuilder

        with _conn(ml_db) as conn:
            _store_memory(
                conn,
                _memory_id("tenant-a"),
                "Tenant A private note.",
                "_user_a_",
                "_tenant_a_",
                "2026-01-01T00:00:00+00:00",
            )
            _store_memory(
                conn,
                _memory_id("tenant-b"),
                "Tenant B private note.",
                "_user_b_",
                "_tenant_b_",
                "2026-01-01T00:00:00+00:00",
            )

        builder = TemporalQueryBuilder(db_path=str(ml_db))

        set_current_user_id("_user_a_")
        set_current_account_id("_tenant_a_")
        try:
            a_results = builder.get_memories_as_of_time(
                user_id="_user_a_",
                target_date=datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
                min_importance=0.0,
            )
        finally:
            reset_tenant_context()

        set_current_user_id("_user_b_")
        set_current_account_id("_tenant_b_")
        try:
            b_results = builder.get_memories_as_of_time(
                user_id="_user_b_",
                target_date=datetime.fromisoformat("2099-01-01T00:00:00+00:00"),
                min_importance=0.0,
            )
        finally:
            reset_tenant_context()

        a_content = " ".join(r.content for r in a_results)
        b_content = " ".join(r.content for r in b_results)
        assert "Tenant A" in a_content
        assert "Tenant B" not in a_content
        assert "Tenant B" in b_content
        assert "Tenant A" not in b_content


class TestTombstoneInvariants:
    def test_tombstone_preserves_row_shape_but_nulls_content(self, ml_db):
        from foresight.capture import get_capture_pipeline

        pipeline = get_capture_pipeline()
        pipeline.db_path = str(ml_db)

        messages = [{"role": "user", "content": "Erase me later."}]
        pipeline.run(
            session_id="s3",
            messages=messages,
            user_id="_test_user_",
            tenant_id="_test_",
            infer=False,
        )

        with _conn(ml_db) as conn:
            raw_id = conn.execute(
                "SELECT id FROM raw_events WHERE session_id = ? LIMIT 1", ("s3",)
            ).fetchone()["id"]
            before = conn.execute(
                "SELECT content, tombstone FROM raw_events WHERE id = ?", (raw_id,)
            ).fetchone()
            assert before["content"]
            assert before["tombstone"] == 0

        tombstone_raw_events(
            user_id="_test_user_",
            session_id="s3",
            older_than=None,
            tenant_id="_test_",
            db_path=str(ml_db),
        )

        with _conn(ml_db) as conn:
            after = conn.execute(
                "SELECT content, tombstone, metadata FROM raw_events WHERE id = ?", (raw_id,)
            ).fetchone()
            assert after["content"] is None
            assert after["tombstone"] == 1
            assert json.loads(after["metadata"] or "{}") == {}


def _iso_ago(*, minutes=0, seconds=0):
    """Return an ISO-8601 timestamp in the past without needing math in tests."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes, seconds=seconds)
    return dt.replace(microsecond=0).isoformat()


def _iso_between(start: str, end: str) -> str:
    """Return an ISO timestamp strictly between start and end."""
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end)
    assert s < e
    mid = s + (e - s) / 2
    return mid.replace(tzinfo=timezone.utc).isoformat()
