"""Tests for the PIX-4701 retrieval-quality work.

Covers:

- The rerank gate (``foresight.reranker``): env flag parsing, bounded
  score->multiplier mapping, cached/no-op reranker resolution.
- The ``FastEmbedReranker`` wrapper against a stubbed fastembed package:
  dict-item ordering, float passthrough, empty docs, model propagation.
- The retriever rerank stage: promotion of an under-ranked candidate,
  field/signal_counts population, no-op when disabled or unavailable,
  early-termination suppression, and rerank-state cache-key isolation.
- The ``explain=True`` score breakdown (score_details) with rerank off /
  applied / unavailable, to_dict gating, and the empty-results case.
- Migration v17: pgvector column + HNSW index on Postgres (dual path);
  recorded-but-skipped on SQLite.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight import hybrid_retriever as hr, reranker as reranker_module
from foresight.backend import SCHEMA_MIGRATIONS
from foresight.backend.backend_migrations import current_version, run_migrations
from foresight.backend.schema_ddl import PGVECTOR_MIGRATION
from foresight.hybrid_retriever import HybridRetriever
from tests._sqlite_backend import SqliteBackend
from tests.test_hybrid_retriever import create_test_db

USER_ID = "test_user"
QUERY = "work"
BOOST_CONTENT = "work conference call notes from last week"
BOOST_SCORE = 6.0
PENALTY_SCORE = -6.0

# Keyword-only search options: every non-keyword signal off. tfidf is turned
# off via use_tfidf_cosine (not the use_semantic alias) so that
# _try_early_termination's raw-option checks also see it as disabled.
KEYWORD_ONLY: dict[str, object] = {
    "use_tfidf_cosine": False,
    "use_vector": False,
    "use_graph": False,
    "use_temporal": False,
}

# The three work memories below are installed on top of create_test_db()'s
# mem_1..mem_5. Keyword scoring is substring-count / whitespace-token-count
# against the query "work", giving:
#   mem_5     "Work deadline approaching, feeling overwhelmed"    -> 1/5 = 0.2000
#   mem_work1 "work stress kept me up late"                       -> 1/6 = 0.1667
#   mem_work3 "work conference call notes from last week"         -> 1/7 = 0.1429
#   mem_work2 "planning the work schedule for next quarter now"   -> 1/8 = 0.1250
# so mem_work3 sits at keyword rank 3, outside the limit=2 top slice, and is
# promoted to #1 by the +6.0 rerank boost.
#
# Decay is neutralized (current_strength == importance for every candidate,
# including the fixture's mem_5) so the strength-decay multiplier is 1.0 and
# the combined ordering equals the fused ordering the math below assumes.
_WORK_MEMORIES = [
    ("mem_work1", "work stress kept me up late", 1),
    ("mem_work2", "planning the work schedule for next quarter now", 2),
    ("mem_work3", BOOST_CONTENT, 3),
]


def _install_work_memories(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        now = datetime.now(timezone.utc)
        for mid, content, minutes_ago in _WORK_MEMORIES:
            ts = (now - timedelta(minutes=minutes_ago)).isoformat()
            conn.execute(
                "INSERT INTO memories (id, user_id, tenant_id, content, category, importance,"
                " current_strength, strength_trend, created_at, accessed_at)"
                " VALUES (?, ?, 'default', ?, 'fact', 0.5, 0.5, 'stable', ?, ?)",
                (mid, USER_ID, content, ts, ts),
            )
        conn.execute("UPDATE memories SET current_strength = 0.9 WHERE id = 'mem_5'")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def work_db():
    path = create_test_db()
    _install_work_memories(path)
    yield path
    os.unlink(path)


class FakeReranker:
    """Content-keyed reranker stub satisfying the Reranker protocol."""

    provider_name = "fake"
    model = "fake-model"

    def __init__(self, scores: dict[str, float] | None = None, default: float = PENALTY_SCORE):
        self.scores = dict(scores or {})
        self.default = default
        self.calls: list[tuple[str, list[str]]] = []

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.calls.append((query, list(documents)))
        return [self.scores.get(doc, self.default) for doc in documents]


def _search(
    db_path: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rerank_on: bool,
    reranker=None,
    limit: int = 2,
    **overrides,
):
    """Run a keyword-only search with the rerank gate forced to a known state."""
    monkeypatch.setattr(hr, "rerank_active", lambda: rerank_on)
    monkeypatch.setattr(hr, "get_reranker", lambda: reranker)
    retriever = HybridRetriever(db_path)
    options = {**KEYWORD_ONLY, **overrides}
    return retriever.search(QUERY, USER_ID, limit=limit, **options)


# =============================================================================
# Rerank gate: env parsing, multiplier math, provider resolution
# =============================================================================


class TestRerankGate:
    def test_rerank_enabled_off_by_default(self, monkeypatch):
        monkeypatch.delenv("FORESIGHT_RERANK_ENABLED", raising=False)
        assert reranker_module.rerank_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", " on ", "True"])
    def test_rerank_enabled_truthy_values(self, monkeypatch, value):
        monkeypatch.setenv("FORESIGHT_RERANK_ENABLED", value)
        assert reranker_module.rerank_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "enabled"])
    def test_rerank_enabled_falsy_values(self, monkeypatch, value):
        monkeypatch.setenv("FORESIGHT_RERANK_ENABLED", value)
        assert reranker_module.rerank_enabled() is False

    def test_multiplier_is_one_at_zero_score(self):
        assert reranker_module.score_to_multiplier(0.0) == pytest.approx(1.0)

    def test_multiplier_bounded_by_half_band(self):
        # Float64 saturates sigmoid at extreme scores, so the band is
        # inclusive: [0.5, 1.5].
        high = reranker_module.score_to_multiplier(500.0)
        low = reranker_module.score_to_multiplier(-500.0)
        assert 1.49 < high <= 1.5
        assert 0.5 <= low < 0.51
        assert low < high
        # Moderate scores stay strictly inside the band.
        assert reranker_module.MULTIPLIER_FLOOR < reranker_module.score_to_multiplier(3.0) < 1.5

    def test_multiplier_monotonic_in_score(self):
        low = reranker_module.score_to_multiplier(-3.0)
        mid = reranker_module.score_to_multiplier(0.0)
        high = reranker_module.score_to_multiplier(3.0)
        assert low < mid < high

    def test_get_reranker_none_when_disabled(self, monkeypatch):
        monkeypatch.delenv("FORESIGHT_RERANK_ENABLED", raising=False)
        assert reranker_module.get_reranker() is None

    def test_get_reranker_none_when_dependency_missing(self, monkeypatch):
        monkeypatch.setenv("FORESIGHT_RERANK_ENABLED", "1")
        monkeypatch.setattr(reranker_module, "_fastembed_rerank_available", lambda: False)
        assert reranker_module.get_reranker() is None

    def test_get_reranker_none_for_unknown_provider(self, monkeypatch):
        monkeypatch.setenv("FORESIGHT_RERANK_ENABLED", "1")
        assert reranker_module.get_reranker(provider="bogus") is None

    def test_rerank_active_false_when_unavailable(self, monkeypatch):
        monkeypatch.setenv("FORESIGHT_RERANK_ENABLED", "1")
        monkeypatch.setattr(reranker_module, "_fastembed_rerank_available", lambda: False)
        assert reranker_module.rerank_active() is False


# =============================================================================
# FastEmbedReranker against a stubbed fastembed package
# =============================================================================


class _StubCrossEncoder:
    """Minimal fastembed CrossEncoder double with a deterministic score map."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.return_dicts = False
        self.calls: list[tuple[str, list[str]]] = []

    def rerank(self, query: str, documents: list[str]) -> list:
        self.calls.append((query, list(documents)))
        if self.return_dicts:
            return [{"index": i, "score": float(len(documents) - i)} for i in range(len(documents))]
        return [float(i) for i in range(len(documents))]


def _install_fastembed_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    fastembed = types.ModuleType("fastembed")
    rerank_pkg = types.ModuleType("fastembed.rerank")
    cross_mod = types.ModuleType("fastembed.rerank.cross_encoder")
    cross_mod.CrossEncoder = _StubCrossEncoder
    fastembed.rerank = rerank_pkg
    rerank_pkg.cross_encoder = cross_mod
    monkeypatch.setitem(sys.modules, "fastembed", fastembed)
    monkeypatch.setitem(sys.modules, "fastembed.rerank", rerank_pkg)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", cross_mod)
    monkeypatch.setattr(reranker_module, "_fastembed_rerank_available", lambda: True)


class TestFastEmbedReranker:
    @pytest.fixture(autouse=True)
    def _clear_reranker_cache(self):
        reranker_module._RERANKER_CACHE.clear()
        yield
        reranker_module._RERANKER_CACHE.clear()

    def test_returns_plain_float_scores_in_document_order(self, monkeypatch):
        _install_fastembed_stub(monkeypatch)
        reranker = reranker_module.FastEmbedReranker()
        assert reranker.rerank("q", ["a", "bb", "ccc"]) == [0.0, 1.0, 2.0]

    def test_dict_items_restored_to_document_order(self, monkeypatch):
        _install_fastembed_stub(monkeypatch)
        reranker = reranker_module.FastEmbedReranker()
        reranker._encoder.return_dicts = True
        # The stub emits {"index": i, "score": n - i}; scores must come back
        # aligned with document order (index 0 first), not score order.
        assert reranker.rerank("q", ["a", "bb", "ccc"]) == [3.0, 2.0, 1.0]

    def test_empty_documents_short_circuits_without_calling_encoder(self, monkeypatch):
        _install_fastembed_stub(monkeypatch)
        reranker = reranker_module.FastEmbedReranker()
        assert reranker.rerank("q", []) == []
        assert reranker._encoder.calls == []

    def test_model_propagation_and_default(self, monkeypatch):
        _install_fastembed_stub(monkeypatch)
        custom = reranker_module.FastEmbedReranker(model="acme/rerank-x")
        assert custom.model == "acme/rerank-x"
        assert custom._encoder.model_name == "acme/rerank-x"
        default = reranker_module.FastEmbedReranker()
        assert default.model == reranker_module.DEFAULT_RERANKER_MODEL
        assert default._encoder.model_name == reranker_module.DEFAULT_RERANKER_MODEL

    def test_get_reranker_returns_cached_instance(self, monkeypatch):
        _install_fastembed_stub(monkeypatch)
        monkeypatch.setenv("FORESIGHT_RERANK_ENABLED", "1")
        first = reranker_module.get_reranker()
        second = reranker_module.get_reranker()
        assert first is not None
        assert first is second
        assert isinstance(first, reranker_module.FastEmbedReranker)

    def test_get_reranker_init_failure_degrades_to_none(self, monkeypatch):
        _install_fastembed_stub(monkeypatch)
        monkeypatch.setenv("FORESIGHT_RERANK_ENABLED", "1")

        def boom():
            raise RuntimeError("model download failed")

        monkeypatch.setattr(reranker_module, "FastEmbedReranker", boom)
        assert reranker_module.get_reranker() is None


# =============================================================================
# Retriever rerank stage
# =============================================================================


class TestRetrieverRerankStage:
    def test_promotes_boosted_candidate_into_top_n(self, work_db, monkeypatch):
        # Control: no rerank — mem_work3 sits outside the raw top-2 (rank 3).
        control = _search(work_db, monkeypatch, rerank_on=False)
        assert [r.memory_id for r in control.results] == ["mem_5", "mem_work1"]
        assert all(r.rerank_score is None for r in control.results)
        assert all(r.rerank_multiplier == 1.0 for r in control.results)
        assert "rerank" not in control.signal_counts

        # Rerank: the cross-encoder boosts mem_work3 (+6.0) and penalizes
        # everything else (-6.0), promoting it from rank 3 to #1.
        fake = FakeReranker(scores={BOOST_CONTENT: BOOST_SCORE})
        reranked = _search(work_db, monkeypatch, rerank_on=True, reranker=fake)
        assert [r.memory_id for r in reranked.results] == ["mem_work3", "mem_5"]
        assert reranked.total_candidates == 4
        assert reranked.signal_counts["keyword"] == 4

        top = reranked.results[0]
        assert top.rerank_score == pytest.approx(BOOST_SCORE)
        assert top.rerank_multiplier == pytest.approx(1.4975, abs=1e-3)
        assert top.rrf_score > 0

        penalized = reranked.results[1]
        assert penalized.rerank_score == pytest.approx(PENALTY_SCORE)
        assert penalized.rerank_multiplier == pytest.approx(0.5025, abs=1e-3)

        assert reranked.signal_counts["rerank"] == "fake"
        assert fake.calls, "reranker should have been invoked"
        assert fake.calls[0][0] == QUERY
        assert BOOST_CONTENT in fake.calls[0][1]

    def test_enabled_but_unavailable_is_a_noop(self, work_db, monkeypatch):
        control = _search(work_db, monkeypatch, rerank_on=False)
        noop = _search(work_db, monkeypatch, rerank_on=True, reranker=None)
        assert [r.memory_id for r in noop.results] == [r.memory_id for r in control.results]
        assert all(r.rerank_score is None for r in noop.results)
        assert all(r.rerank_multiplier == 1.0 for r in noop.results)
        assert "rerank" not in noop.signal_counts

    def test_early_termination_suppressed_while_reranking(self, work_db, monkeypatch):
        # The default 2.0 ratio is unreachable under keyword-only RRF (adjacent
        # fused ranks differ by ~1.6%), so lower it to make the shortcut fire.
        monkeypatch.setattr(hr, "FAST_PATH_EARLY_TERMINATION_RATIO", 1.0)
        baseline = _search(work_db, monkeypatch, rerank_on=False)
        assert baseline.signal_counts["fast_path"] == "early_termination"

        reranking = _search(work_db, monkeypatch, rerank_on=True, reranker=None)
        assert "fast_path" not in reranking.signal_counts

    def test_cache_isolates_rerank_state(self, work_db, monkeypatch):
        retriever = HybridRetriever(work_db)

        monkeypatch.setattr(hr, "rerank_active", lambda: False)
        first = retriever.search(QUERY, USER_ID, limit=2, **KEYWORD_ONLY)
        ids_first = [r.memory_id for r in first.results]
        assert first.signal_counts.get("fast_path") != "cache"

        # Rerank state is part of the cache key: enabling it must force a
        # fresh computation, not serve the cached un-reranked result.
        monkeypatch.setattr(hr, "rerank_active", lambda: True)
        monkeypatch.setattr(hr, "get_reranker", lambda: FakeReranker(scores={BOOST_CONTENT: BOOST_SCORE}))
        second = retriever.search(QUERY, USER_ID, limit=2, **KEYWORD_ONLY)
        assert second.signal_counts.get("fast_path") != "cache"
        assert [r.memory_id for r in second.results] == ["mem_work3", "mem_5"]

        # Back to the off state: the original cached result is reused.
        monkeypatch.setattr(hr, "rerank_active", lambda: False)
        monkeypatch.setattr(hr, "get_reranker", lambda: None)
        third = retriever.search(QUERY, USER_ID, limit=2, **KEYWORD_ONLY)
        assert third.signal_counts["fast_path"] == "cache"
        assert [r.memory_id for r in third.results] == ids_first


# =============================================================================
# Explain output (score_details)
# =============================================================================


class TestExplainScoreDetails:
    def test_score_details_shape_without_rerank(self, work_db, monkeypatch):
        result = _search(work_db, monkeypatch, rerank_on=False, explain=True)
        details = result.score_details
        assert details is not None
        assert details["weights"] == {"keyword": 1.0, "semantic": 0.7, "vector": 0.9, "graph": 0.8, "temporal": 0.8}
        assert details["rrf_k"] == 60.0
        assert details["min_importance_threshold"] == pytest.approx(0.1)
        assert details["rerank"] == {"enabled": False, "applied": False, "provider": None}
        # Only the keyword signal contributed candidates, so only its weight
        # contributes to the max achievable fused score (1/(k+1) at rank 1).
        keyword_max = round(1.0 / 61.0, 6)
        assert details["per_signal_max"] == {"keyword": keyword_max}
        assert details["max_possible_score"] == keyword_max

    def test_score_details_rerank_applied(self, work_db, monkeypatch):
        fake = FakeReranker(scores={BOOST_CONTENT: BOOST_SCORE})
        result = _search(work_db, monkeypatch, rerank_on=True, reranker=fake, explain=True)
        assert result.score_details is not None
        assert result.score_details["rerank"] == {"enabled": True, "applied": True, "provider": "fake"}
        assert result.results[0].memory_id == "mem_work3"
        assert result.results[0].rerank_score == pytest.approx(BOOST_SCORE)

    def test_score_details_rerank_enabled_but_unavailable(self, work_db, monkeypatch):
        result = _search(work_db, monkeypatch, rerank_on=True, reranker=None, explain=True)
        assert result.score_details is not None
        assert result.score_details["rerank"] == {"enabled": True, "applied": False, "provider": None}

    def test_to_dict_gates_score_details(self, work_db, monkeypatch):
        plain = _search(work_db, monkeypatch, rerank_on=False)
        assert plain.score_details is None
        assert "score_details" not in plain.to_dict()

        explained = _search(work_db, monkeypatch, rerank_on=False, explain=True)
        assert "score_details" in explained.to_dict()

    def test_empty_results_still_explain(self, work_db, monkeypatch):
        monkeypatch.delenv("FORESIGHT_RERANK_ENABLED", raising=False)
        retriever = HybridRetriever(work_db)
        result = retriever.search("zzznonexistentterm", USER_ID, limit=2, **{**KEYWORD_ONLY, "explain": True})
        assert result.results == []
        assert result.total_candidates == 0
        details = result.score_details
        assert details is not None
        assert details["per_signal_max"] == {}
        assert details["max_possible_score"] == 0.0
        assert details["rerank"] == {"enabled": False, "applied": False, "provider": None}
        assert "score_details" in result.to_dict()


# =============================================================================
# Migration v17 (pgvector ANN)
# =============================================================================


class TestPgvectorMigrationSqlite:
    """v17 is Postgres-only: SQLite records the version but adds no column."""

    def test_v17_recorded_without_vector_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = SqliteBackend(db_path=os.path.join(tmp, "foresight.sqlite"))
            backend.connect()
            try:
                applied = run_migrations(backend)
                assert PGVECTOR_MIGRATION in applied
                assert current_version(backend) == max(SCHEMA_MIGRATIONS)
                assert backend.column_exists("memory_embeddings", "embedding") is False
            finally:
                backend.close()


@pytest.mark.skipif(
    os.environ.get("FORESIGHT_DB_URL_TEST") is None,
    reason="FORESIGHT_DB_URL_TEST not set; skipping Postgres pgvector test",
)
class TestPgvectorMigrationPostgres:
    def test_v17_embedding_column_and_hnsw_index(self):
        dsn = os.environ["FORESIGHT_DB_URL_TEST"]
        from foresight.backend.postgres_backend import PostgresBackend

        backend = PostgresBackend(dsn=dsn)
        backend.connect()
        try:
            backend.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
            backend.execute("DROP TABLE IF EXISTS memory_embeddings CASCADE")
            applied = run_migrations(backend)
            assert PGVECTOR_MIGRATION in applied
            assert current_version(backend) == max(SCHEMA_MIGRATIONS)

            has_pgvector = backend.fetch_one("SELECT 1 FROM pg_extension WHERE extname = 'vector'") is not None
            if has_pgvector:
                columns = backend.fetch(
                    "SELECT udt_name FROM information_schema.columns"
                    " WHERE table_name = 'memory_embeddings' AND column_name = 'embedding'"
                )
                assert columns, "expected the embedding column when pgvector is available"
                assert columns[0]["udt_name"] == "vector"
                indexes = backend.fetch("SELECT 1 FROM pg_indexes WHERE indexname = 'idx_memory_embeddings_hnsw_384'")
                assert indexes, "expected the 384-dim HNSW index after v17"
            else:
                # Graceful degradation: version recorded, no vector column,
                # semantic search falls back to the Python cosine scan.
                assert not backend.column_exists("memory_embeddings", "embedding")
        finally:
            backend.close()
