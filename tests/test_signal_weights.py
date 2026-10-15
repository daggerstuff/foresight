"""
Tests for per-request signal weight overrides (PIX-4730).

The global RRF tuning stays unchanged when no overrides are supplied; when
``signal_weights`` is provided on ``HybridSearchOptions`` it overrides the
corresponding signal weights for that request only and is reflected in the
explain output.
"""

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight.hybrid_retriever import (
    VALID_SIGNAL_WEIGHT_KEYS,
    HybridRetriever,
    HybridSearchOptions,
    reset_hybrid_retriever,
)


@pytest.fixture(autouse=True)
def cleanup():
    reset_hybrid_retriever()
    yield
    reset_hybrid_retriever()


def _create_test_db() -> str:
    """Create a temp DB where keyword and temporal rankings disagree."""
    fd, path = tempfile.mkstemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            tenant_id TEXT DEFAULT 'default',
            scope TEXT DEFAULT 'session',
            retention TEXT DEFAULT 'short_term',
            content TEXT,
            tags TEXT DEFAULT '[]',
            emotional_context TEXT DEFAULT '{}',
            metrics TEXT DEFAULT '{}',
            gist TEXT,
            vector_id TEXT,
            is_ghost INTEGER DEFAULT 0,
            synthesized_from TEXT DEFAULT '[]',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            category TEXT,
            importance REAL DEFAULT 0.5,
            current_strength REAL DEFAULT 0.5,
            decay_rate REAL DEFAULT 1.0,
            activation_count INTEGER DEFAULT 0,
            strength_trend TEXT DEFAULT 'stable',
            accessed_at TEXT,
            last_retrieved_at TEXT,
            is_latest INTEGER DEFAULT 1,
            inferred INTEGER DEFAULT 0,
            superseded_by TEXT
        )
    """)

    uid = "signal_weights_user"
    now = datetime.now(timezone.utc)

    # ``old_match`` is keyword-relevant but old; ``recent_miss`` is recent but
    # contains none of the query terms. This makes keyword and temporal rankings
    # disagree, so swapping their weights swaps the overall top result.
    old_match = (
        "mem_old_match",
        "Project firewall rules were carefully reviewed today",
        now - timedelta(days=30),
    )
    recent_miss = (
        "mem_recent_miss",
    "I went out for lunch and forgot to bring my umbrella",
        now - timedelta(hours=1),
    )

    for mid, content, ts in (old_match, recent_miss):
        conn.execute(
            """
            INSERT INTO memories
            (id, user_id, tenant_id, content, category, importance, current_strength,
             strength_trend, created_at, accessed_at)
            VALUES (?, ?, 'default', ?, 'fact', 0.7, 0.7, 'stable', ?, ?)
            """,
            (mid, uid, content, ts.isoformat(), ts.isoformat()),
        )

    conn.commit()
    conn.close()
    os.close(fd)
    return path


@pytest.fixture
def swapped_db():
    path = _create_test_db()
    yield path
    os.unlink(path)


class TestPerRequestSignalWeights:
    """Per-request weight overrides affect fusion but not global config."""

    def test_absent_weights_match_default_retriever_weights(self, swapped_db):
        """A search with no overrides must behave like the default-weight search."""
        retriever = HybridRetriever(swapped_db)

        default_result = retriever.search(
            "firewall",
            "signal_weights_user",
            options=HybridSearchOptions(
                limit=2,
                use_vector=False,
                use_graph=False,
                use_tfidf_cosine=False,
                explain=True,
            ),
        )
        explicit_result = retriever.search(
            "firewall",
            "signal_weights_user",
            options=HybridSearchOptions(
                limit=2,
                use_vector=False,
                use_graph=False,
                use_tfidf_cosine=False,
                explain=True,
                signal_weights={
                    "keyword": 1.0,
                    "temporal": 0.8,
                },
            ),
        )
        assert default_result.total_candidates == explicit_result.total_candidates
        assert [r.memory_id for r in default_result.results] == [
            r.memory_id for r in explicit_result.results
        ]

    def test_weights_change_fusion_ordering(self, swapped_db):
        """Boosting temporal over keyword makes the recent memory rank first."""
        retriever = HybridRetriever(swapped_db)

        default_result = retriever.search(
            "firewall",
            "signal_weights_user",
            options=HybridSearchOptions(
                limit=2,
                use_vector=False,
                use_graph=False,
                use_tfidf_cosine=False,
                explain=True,
            ),
        )

        weighted_result = retriever.search(
            "firewall",
            "signal_weights_user",
            options=HybridSearchOptions(
                limit=2,
                use_vector=False,
                use_graph=False,
                use_tfidf_cosine=False,
                explain=True,
                signal_weights={"temporal": 50.0, "keyword": 0.01},
            ),
        )

        # Default ordering: keyword signal dominates, so the keyword match is first.
        assert [r.memory_id for r in default_result.results[:2]] == [
            "mem_old_match",
            "mem_recent_miss",
        ]

        # Temporal-heavy ordering: recent memory wins despite no keyword match.
        assert [r.memory_id for r in weighted_result.results[:2]] == [
            "mem_recent_miss",
            "mem_old_match",
        ]

    def test_invalid_signal_weight_key_is_rejected(self, swapped_db):
        """Unknown keys raise ValueError listing valid keys."""
        retriever = HybridRetriever(swapped_db)

        with pytest.raises(ValueError, match="Invalid signal_weight keys") as exc_info:
            retriever.search(
                "firewall",
                "signal_weights_user",
                options=HybridSearchOptions(
                    limit=2,
                    use_vector=False,
                    use_graph=False,
                    use_tfidf_cosine=False,
                    fast_path_enabled=False,
                    signal_weights={"bogus": 1.0, "also_bad": 2.0},
                ),
            )

        msg = str(exc_info.value)
        assert "Invalid signal_weight keys" in msg
        assert "also_bad" in msg
        assert "bogus" in msg
        for valid in VALID_SIGNAL_WEIGHT_KEYS:
            assert valid in msg

    def test_explain_echoes_effective_weights(self, swapped_db):
        """explain output reflects the per-request weights."""
        retriever = HybridRetriever(swapped_db)

        result = retriever.search(
            "firewall",
            "signal_weights_user",
            options=HybridSearchOptions(
                limit=2,
                use_vector=False,
                use_graph=False,
                use_tfidf_cosine=False,
                explain=True,
                signal_weights={"temporal": 5.0, "keyword": 0.0},
            ),
        )

        assert result.score_details is not None
        weights = result.score_details["weights"]
        assert weights["temporal"] == 5.0
        assert weights["keyword"] == 0.0
        assert weights["semantic"] == pytest.approx(0.7)
        # temporal was active, so its max contribution should be present.
        assert "temporal" in result.score_details["per_signal_max"]

    def test_reciprocal_rank_fusion_uses_explicit_weights(self, swapped_db):
        """The low-level fusion helper can be driven by a weights dict.

        This protects against future changes that accidentally read
        ``self.weights`` instead of the per-request weights in the fusion path.
        """
        retriever = HybridRetriever(swapped_db)
        keyword = {"a": 1}
        temporal = {"b": 1, "a": 2}

        default_fusion = retriever._reciprocal_rank_fusion(
            keyword, {}, {}, temporal, {}
        )
        weighted_fusion = retriever._reciprocal_rank_fusion(
            keyword, {}, {}, temporal, {}, weights={"keyword": 0.0, "temporal": 100.0}
        )

        # With default weights ``temporal`` (0.8) < ``keyword`` (1.0), ``a``
        # (rank 1 keyword + rank 2 temporal) outranks ``b`` (rank 1 temporal
        # only). With temporal boosted to 100.0, the temporal-only ``b`` wins.
        default_order = [mid for mid, _ in default_fusion]
        weighted_order = [mid for mid, _ in weighted_fusion]
        assert "a" in default_order
        assert "b" in default_order
        assert default_order[0] == "a"
        assert weighted_order[0] == "b"
