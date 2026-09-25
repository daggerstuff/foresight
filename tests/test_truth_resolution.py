"""Tests for PIX-4702 truth resolution (write-time supersede detection).

Covers:

- The pure detector (``foresight.truth_resolution.detect_supersession``):
  sentiment conflicts, negation flips, unrelated/rephrased content.
- The feature gate (``FORESIGHT_SUPERSEDE_DETECTION``, off by default).
- Store-path integration: storing a contradicting memory marks the older
  memory ``is_latest = 0`` / ``superseded_by`` and creates an ``updates`` edge.
- Migration v15 columns (is_latest, inferred, superseded_by) exist with safe
  defaults and the partial index is created.
- Capture pipeline flags derived (near-duplicate) memories as ``inferred``.
- Retriever penalties: superseded and inferred memories are down-ranked but
  never filtered out of results.
"""

import re
import sqlite3

import pytest

from foresight.hybrid_retriever import (
    INFERRED_RANK_PENALTY,
    SUPERSEDED_RANK_PENALTY,
    HybridRetriever,
    reset_hybrid_retriever,
)
from foresight.truth_resolution import (
    OVERLAP_THRESHOLD,
    detect_supersession,
    supersede_detection_enabled,
)
from tests.test_hybrid_retriever import create_test_db

# ====== Pure detector tests (no DB) ======


class TestDetectSupersession:
    def test_sentiment_conflict_detected(self):
        result = detect_supersession(
            "Therapy feels helpful for my weekly sessions",
            "Therapy feels harmful for my weekly sessions",
        )
        assert result is not None
        assert result.signal == "sentiment_conflict"
        assert result.overlap >= OVERLAP_THRESHOLD
        assert 0.0 < result.confidence <= 1.0

    def test_negation_flip_detected(self):
        result = detect_supersession(
            "I stopped going to the gym every morning",
            "I go to the gym every morning",
        )
        assert result is not None
        assert result.signal == "negation_flip"

    def test_unrelated_content_not_flagged(self):
        assert (
            detect_supersession(
                "User prefers FastAPI for building backend services",
                "Client enjoys hiking on weekends",
            )
            is None
        )

    def test_rephrase_not_flagged(self):
        """High overlap without a contradiction signal is a restatement."""
        assert (
            detect_supersession(
                "I love going to the gym every morning",
                "I really love going to the gym every single morning",
            )
            is None
        )

    def test_marker_in_old_only_not_flagged(self):
        """Negation flip is asymmetric: restating a quitted state is not new."""
        assert (
            detect_supersession(
                "I stopped going to the gym every morning",
                "I stopped going to the gym every morning before work",
            )
            is None
        )

    def test_low_overlap_not_flagged(self):
        assert detect_supersession("I quit my job at the office", "The sky is blue today") is None

    def test_confidence_formula(self):
        result = detect_supersession(
            "Therapy feels helpful for my weekly sessions",
            "Therapy feels harmful for my weekly sessions",
        )
        assert result is not None
        assert result.confidence == min(result.overlap * 1.5, 1.0)


class TestSupersedeGate:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("FORESIGHT_SUPERSEDE_DETECTION", raising=False)
        assert supersede_detection_enabled() is False

    def test_on_when_enabled(self, monkeypatch):
        for value in ("1", "true", "yes", "on"):
            monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", value)
            assert supersede_detection_enabled() is True

    def test_junk_values_disabled(self, monkeypatch):
        monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "0")
        assert supersede_detection_enabled() is False
        monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "")
        assert supersede_detection_enabled() is False


# ====== Fixtures ======


@pytest.fixture
def pg_db(monkeypatch):
    """Postgres test backend (conftest session fixture) with tenant context and
    the ``sqlite3.connect("postgres")`` mock from test_server.py, so store-path
    tests can query the same DB the server writes to via _handle_memory_store."""
    from foresight.memory_relationships import reset_memory_relationship_store
    from foresight.tenant_context import set_current_account_id, set_current_user_id

    set_current_user_id("_test_user_")
    set_current_account_id("_test_")
    reset_memory_relationship_store()

    real_sqlite3_connect = sqlite3.connect

    def mock_sqlite3_connect(database, *args, **kwargs):
        if database == "postgres":
            from foresight.server import PostgresPooledConnection, _global_backend

            pool = _global_backend._pool
            return PostgresPooledConnection(pool.getconn(), pool)
        return real_sqlite3_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", mock_sqlite3_connect)

    yield

    reset_memory_relationship_store()
    from foresight.tenant_context import reset_tenant_context

    reset_tenant_context()


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """SQLite backend for the capture pipeline, which uses connection_pool
    directly (same pattern as test_capture.py) rather than get_db_connection."""
    db_file = tmp_path / "test_truth_resolution_capture.db"
    monkeypatch.setenv("FORESIGHT_DB_PATH", str(db_file))

    import foresight.config as config_module
    import foresight.connection_pool as conn_pool_module
    from foresight.capture import get_capture_pipeline, reset_capture_pipeline
    from foresight.connection_pool import reset_pool
    from foresight.memory_relationships import reset_memory_relationship_store
    from foresight.server import init_db
    from foresight.tenant_context import set_current_account_id, set_current_user_id
    from tests._sqlite_backend import SqliteBackend

    monkeypatch.setattr(config_module, "DB_PATH", str(db_file))
    monkeypatch.setattr(conn_pool_module, "DB_PATH", str(db_file))
    import foresight.server as server_module

    monkeypatch.setattr(server_module, "DB_PATH", str(db_file))
    import foresight.subconscious as subconscious_module

    monkeypatch.setattr(subconscious_module, "DB_PATH", str(db_file))
    reset_pool()

    set_current_user_id("_test_user_")
    set_current_account_id("_test_")

    init_db(backend=SqliteBackend(db_path=str(db_file)))
    reset_capture_pipeline()
    pipeline = get_capture_pipeline()
    pipeline.db_path = str(db_file)
    reset_memory_relationship_store()
    yield db_file
    reset_pool()
    from foresight.tenant_context import reset_tenant_context

    reset_tenant_context()


@pytest.fixture(autouse=True)
def _reset_retriever():
    reset_hybrid_retriever()
    yield
    reset_hybrid_retriever()


def _stored_id(res: str) -> str:
    assert res.startswith("Stored memory"), res
    match = re.search(r"Stored memory (\S+?)\.", res)
    assert match, res
    return match.group(1)


def _pg():
    """DB handle over the mocked sqlite3.connect("postgres") shim."""
    return sqlite3.connect("postgres")


# ====== Store-path integration ======


@pytest.mark.usefixtures("pg_db")
class TestStorePathSupersede:
    def test_contradiction_supersedes_old_memory(self, monkeypatch):
        monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "1")
        from foresight.server import manage_memories

        res_old = manage_memories(
            action="store", content="Therapy feels helpful for my weekly sessions", category="fact"
        )
        old_id = _stored_id(res_old)
        res_new = manage_memories(
            action="store", content="Therapy feels harmful for my weekly sessions", category="fact"
        )
        new_id = _stored_id(res_new)
        assert old_id != new_id

        conn = _pg()
        try:
            old_row = conn.execute("SELECT is_latest, superseded_by FROM memories WHERE id = ?", (old_id,)).fetchone()
            new_row = conn.execute("SELECT is_latest, inferred FROM memories WHERE id = ?", (new_id,)).fetchone()
            edge = conn.execute(
                "SELECT confidence, metadata FROM memory_relationships "
                "WHERE relationship_type = 'updates' "
                "AND source_memory_id = ? AND target_memory_id = ?",
                (new_id, old_id),
            ).fetchone()
        finally:
            conn.close()

        assert old_row["is_latest"] == 0
        assert old_row["superseded_by"] == new_id
        assert new_row["is_latest"] == 1
        assert new_row["inferred"] == 0
        assert edge is not None
        assert 0.0 < edge["confidence"] <= 1.0

    def test_negation_flip_supersedes_old_memory(self, monkeypatch):
        monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "1")
        from foresight.server import manage_memories

        res_old = manage_memories(action="store", content="I go to the gym every morning", category="fact")
        old_id = _stored_id(res_old)
        res_new = manage_memories(action="store", content="I stopped going to the gym every morning", category="fact")
        new_id = _stored_id(res_new)

        conn = _pg()
        try:
            old_row = conn.execute("SELECT is_latest, superseded_by FROM memories WHERE id = ?", (old_id,)).fetchone()
            edge = conn.execute(
                "SELECT id FROM memory_relationships WHERE relationship_type = 'updates' "
                "AND source_memory_id = ? AND target_memory_id = ?",
                (new_id, old_id),
            ).fetchone()
        finally:
            conn.close()

        assert old_row["is_latest"] == 0
        assert old_row["superseded_by"] == new_id
        assert edge is not None

    def test_unrelated_memories_not_superseded(self, monkeypatch):
        monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "1")
        from foresight.server import manage_memories

        res1 = manage_memories(
            action="store", content="User prefers FastAPI for building backend services", category="fact"
        )
        res2 = manage_memories(action="store", content="Client enjoys hiking on weekends", category="fact")
        id1, id2 = _stored_id(res1), _stored_id(res2)

        conn = _pg()
        try:
            rows = {r["id"]: r["is_latest"] for r in conn.execute("SELECT id, is_latest FROM memories").fetchall()}
            edges = conn.execute("SELECT id FROM memory_relationships WHERE relationship_type = 'updates'").fetchall()
        finally:
            conn.close()

        assert rows[id1] == 1
        assert rows[id2] == 1
        assert edges == []

    def test_gate_off_no_supersede(self, monkeypatch):
        """With the gate off (default) contradicting memories coexist."""
        monkeypatch.delenv("FORESIGHT_SUPERSEDE_DETECTION", raising=False)
        from foresight.server import manage_memories

        res1 = manage_memories(action="store", content="Therapy feels helpful for my weekly sessions", category="fact")
        res2 = manage_memories(action="store", content="Therapy feels harmful for my weekly sessions", category="fact")
        id1, id2 = _stored_id(res1), _stored_id(res2)

        conn = _pg()
        try:
            rows = {
                r["id"]: (r["is_latest"], r["superseded_by"])
                for r in conn.execute("SELECT id, is_latest, superseded_by FROM memories").fetchall()
            }
            edges = conn.execute("SELECT id FROM memory_relationships WHERE relationship_type = 'updates'").fetchall()
        finally:
            conn.close()

        assert rows[id1] == (1, None)
        assert rows[id2] == (1, None)
        assert edges == []


@pytest.mark.usefixtures("pg_db")
class TestMigrationV15Columns:
    def test_columns_exist_with_safe_defaults(self):
        conn = _pg()
        try:
            rows = conn.execute(
                "SELECT column_name, column_default FROM information_schema.columns WHERE table_name = 'memories'"
            ).fetchall()
        finally:
            conn.close()
        defaults = {r["column_name"]: r["column_default"] for r in rows}
        assert "is_latest" in defaults
        assert "inferred" in defaults
        assert "superseded_by" in defaults
        assert defaults["is_latest"] == "1"
        assert defaults["inferred"] == "0"
        assert defaults["superseded_by"] is None

    def test_index_created(self):
        conn = _pg()
        try:
            rows = conn.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'memories'").fetchall()
        finally:
            conn.close()
        names = {r["indexname"] for r in rows}
        assert "idx_memories_tenant_user_latest" in names

    def test_existing_rows_default_to_latest(self):
        """Rows written without the new columns fall back to safe defaults."""
        conn = _pg()
        try:
            conn.execute(
                "INSERT INTO memories (id, content, created_at) VALUES (?, ?, ?)",
                ("migrate_row", "legacy content", "2026-09-25T00:00:00+00:00"),
            )
            conn.commit()
            row = conn.execute(
                "SELECT is_latest, inferred, superseded_by FROM memories WHERE id = ?",
                ("migrate_row",),
            ).fetchone()
        finally:
            conn.close()
        assert row["is_latest"] == 1
        assert row["inferred"] == 0
        assert row["superseded_by"] is None


class TestCaptureInferredFlag:
    def test_near_dup_derived_memory_flagged_inferred(self, sqlite_db):
        """A memory written with a derives edge (near duplicate) is inferred."""
        from foresight.capture import get_capture_pipeline

        pipeline = get_capture_pipeline()
        pipeline.db_path = str(sqlite_db)
        msgs1 = [
            {"role": "user", "content": "I prefer using FastAPI for building web APIs and backend services."},
            {"role": "assistant", "content": "Good choice for async Python applications with high throughput."},
            {"role": "user", "content": "It has great documentation and auto-generated OpenAPI schemas."},
        ]
        stats1 = pipeline.run("sess_tr_1", msgs1, "_test_user_")
        assert stats1.stored >= 1

        msgs2 = [
            {"role": "user", "content": "I always prefer FastAPI for building REST APIs and web services."},
            {"role": "assistant", "content": "Good, it has excellent async support."},
            {"role": "user", "content": "The auto-generated docs are a big plus."},
        ]
        stats2 = pipeline.run("sess_tr_2", msgs2, "_test_user_")
        assert stats2.stored >= 1

        conn = sqlite3.connect(str(sqlite_db))
        conn.row_factory = sqlite3.Row
        try:
            derives = conn.execute(
                "SELECT source_memory_id, target_memory_id FROM memory_relationships "
                "WHERE relationship_type = 'derives'"
            ).fetchall()
            assert len(derives) >= 1
            for edge in derives:
                source_row = conn.execute(
                    "SELECT inferred, is_latest FROM memories WHERE id = ?", (edge["source_memory_id"],)
                ).fetchone()
                target_row = conn.execute(
                    "SELECT inferred FROM memories WHERE id = ?", (edge["target_memory_id"],)
                ).fetchone()
                assert source_row is not None
                assert source_row["inferred"] == 1, "derived memory must be flagged inferred"
                if target_row is not None:
                    assert target_row["inferred"] == 0, "original memory stays inferred=0"
        finally:
            conn.close()


# ====== Retriever penalty tests (raw sqlite DB, pattern from test_hybrid_retriever) ======


def _mark_row(db_path: str, mid: str, **cols) -> None:
    sets = ", ".join(f"{key} = ?" for key in cols)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"UPDATE memories SET {sets} WHERE id = ?", (*cols.values(), mid))
        conn.commit()
    finally:
        conn.close()


class TestRetrieverTruthPenalties:
    def test_superseded_memory_retrievable_with_penalty(self):
        """Down-ranked, never filtered: a superseded memory still appears."""
        db_path = create_test_db()
        _mark_row(db_path, "mem_4", is_latest=0, superseded_by="mem_new")
        retriever = HybridRetriever(db_path)
        result = retriever.search(
            "family dinner", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        matches = [r for r in result.results if r.memory_id == "mem_4"]
        assert matches, "superseded memory must remain retrievable"
        assert matches[0].truth_multiplier == pytest.approx(SUPERSEDED_RANK_PENALTY)
        assert matches[0].is_latest is False
        assert matches[0].superseded_by == "mem_new"

    def test_latest_ranks_above_superseded(self):
        db_path = create_test_db()
        now_iso = "2026-09-25T00:00:00+00:00"
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO memories (id, user_id, tenant_id, content, category, importance, "
                "strength_trend, created_at, accessed_at, is_latest) "
                "VALUES ('mem_love', 'test_user', 'default', 'I love my new therapist', 'fact', "
                "0.5, 'stable', ?, ?, 1)",
                (now_iso, now_iso),
            )
            conn.execute(
                "INSERT INTO memories (id, user_id, tenant_id, content, category, importance, "
                "strength_trend, created_at, accessed_at, is_latest, superseded_by) "
                "VALUES ('mem_hate', 'test_user', 'default', 'I hate my new therapist', 'fact', "
                "0.5, 'stable', ?, ?, 0, 'mem_love')",
                (now_iso, now_iso),
            )
            conn.commit()
        finally:
            conn.close()

        retriever = HybridRetriever(db_path)
        result = retriever.search(
            "therapist", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        ids = [r.memory_id for r in result.results]
        assert "mem_love" in ids
        assert "mem_hate" in ids, "superseded memory must remain retrievable"
        assert ids.index("mem_love") < ids.index("mem_hate")
        by_id = {r.memory_id: r for r in result.results}
        assert by_id["mem_love"].truth_multiplier == pytest.approx(1.0)
        assert by_id["mem_hate"].truth_multiplier == pytest.approx(SUPERSEDED_RANK_PENALTY)

    def test_inferred_memory_downranked(self):
        db_path = create_test_db()
        _mark_row(db_path, "mem_3", inferred=1)
        retriever = HybridRetriever(db_path)
        result = retriever.search(
            "meditation", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        matches = [r for r in result.results if r.memory_id == "mem_3"]
        assert matches, "inferred memory must remain retrievable"
        assert matches[0].truth_multiplier == pytest.approx(INFERRED_RANK_PENALTY)
        assert matches[0].inferred is True

    def test_clean_memory_full_multiplier(self):
        db_path = create_test_db()
        retriever = HybridRetriever(db_path)
        result = retriever.search(
            "anxious presentation", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        clean = [r for r in result.results if r.memory_id == "mem_1"]
        assert clean
        assert clean[0].truth_multiplier == pytest.approx(1.0)
        assert clean[0].is_latest is True
        assert clean[0].inferred is False

    def test_to_dict_includes_truth_fields(self):
        db_path = create_test_db()
        _mark_row(db_path, "mem_4", is_latest=0, superseded_by="mem_new", inferred=1)
        retriever = HybridRetriever(db_path)
        result = retriever.search(
            "family dinner", "test_user", limit=5, use_graph=False, use_temporal=False, use_semantic=False
        )
        matches = [r for r in result.results if r.memory_id == "mem_4"]
        assert matches
        payload = matches[0].to_dict()
        assert payload["is_latest"] is False
        assert payload["inferred"] is True
        assert payload["superseded_by"] == "mem_new"
        assert payload["truth_multiplier"] == pytest.approx(SUPERSEDED_RANK_PENALTY)
