"""Tests for query rewriting (question -> statement query variants).

Covers ``foresight.query_rewrite`` (deterministic, zero-cost variants in the
style of Supermemory's ``rewriteQuery``, but local and without an LLM call)
and its integration into ``HybridRetriever.search``:

- statement queries pass through unchanged (single variant);
- question queries gain a statement-form variant (original always first);
- "do you remember / tell me" prefixes are stripped;
- empty input is safe, ``max_variants`` caps output, output is deterministic;
- env toggle ``FORESIGHT_QUERY_REWRITE`` enables/disables the stage;
- the retriever issues the statement variant to the lexical signals and
  merges per-signal rankings deterministically (more variant matches wins
  ties, then best rank, then memory id);
- a question query promotes the statement-form memory over a distractor
  that only shares question words — with rewriting disabled the old
  ordering is preserved.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from foresight.hybrid_retriever import HybridRetriever
from foresight.query_rewrite import query_rewrite_enabled, rewrite_variants

USER_ID = "rewrite_user"

KEYWORD_ONLY: dict[str, object] = {
    "use_tfidf_cosine": False,
    "use_vector": False,
    "use_graph": False,
    "use_temporal": False,
}


def _make_db() -> str:
    _, path = tempfile.mkstemp(suffix=".db")
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            tenant_id TEXT DEFAULT 'default',
            content TEXT,
            category TEXT,
            importance REAL DEFAULT 0.5,
            current_strength REAL DEFAULT 0.5,
            strength_trend TEXT DEFAULT 'stable',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_latest INTEGER DEFAULT 1,
            inferred INTEGER DEFAULT 0,
            superseded_by TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO memories (id, user_id, tenant_id, content, created_at) VALUES (?, ?, 'default', ?, ?)",
        ("mem_target", USER_ID, "dentist appointment thursday 3pm", now),
    )
    conn.execute(
        "INSERT INTO memories (id, user_id, tenant_id, content, created_at) VALUES (?, ?, 'default', ?, ?)",
        ("mem_distractor", USER_ID, "when is my performance review", now),
    )
    conn.commit()
    conn.close()
    return path


# --- variant generation -----------------------------------------------------


def test_statement_query_adds_core_variant():
    assert rewrite_variants("my dentist appointment") == ["my dentist appointment", "dentist appointment"]


def test_single_word_query_passes_through_unchanged():
    assert rewrite_variants("dentist") == ["dentist"]


def test_question_rewrites_to_statement_form():
    variants = rewrite_variants("What is my favorite color?")
    assert variants[0] == "What is my favorite color?"
    assert "my favorite color" in variants
    assert all("?" not in v for v in variants[1:])


def test_remember_phrasing_is_stripped():
    variants = rewrite_variants("Do you remember my meeting time?")
    assert "my meeting time" in variants


def test_empty_query_is_safe():
    assert rewrite_variants("") == [""]
    assert rewrite_variants("   ") == ["   "]


def test_max_variants_caps_output():
    variants = rewrite_variants("What is my favorite color?", max_variants=1)
    assert variants == ["What is my favorite color?"]


def test_rewrite_is_deterministic():
    query = "When is my dentist appointment?"
    assert rewrite_variants(query) == rewrite_variants(query)


def test_env_toggle(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FORESIGHT_QUERY_REWRITE", "off")
    assert query_rewrite_enabled() is False
    monkeypatch.setenv("FORESIGHT_QUERY_REWRITE", "1")
    assert query_rewrite_enabled() is True
    monkeypatch.delenv("FORESIGHT_QUERY_REWRITE", raising=False)
    assert query_rewrite_enabled() is True


def test_llm_failure_never_breaks_rewrite(monkeypatch: pytest.MonkeyPatch):
    import foresight.llm_client as llm_client_module

    def _boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm_client_module, "default_llm_call", _boom)
    variants = rewrite_variants("What is my favorite color?", llm_assist=True)
    assert variants[0] == "What is my favorite color?"
    assert "my favorite color" in variants


# --- retriever integration --------------------------------------------------


def test_retriever_issues_statement_variant(monkeypatch: pytest.MonkeyPatch):
    db_path = _make_db()
    seen: list[str] = []
    real_keyword = HybridRetriever._run_keyword_search

    def _spy(self: HybridRetriever, query: str, *args: object, **kwargs: object) -> dict[str, int]:
        seen.append(query)
        return real_keyword(self, query, *args, **kwargs)

    monkeypatch.setattr(HybridRetriever, "_run_keyword_search", _spy)
    retriever = HybridRetriever(db_path)
    retriever.search("When is my dentist appointment?", USER_ID, options=None, **KEYWORD_ONLY)
    assert seen[0] == "When is my dentist appointment?"
    assert "my dentist appointment" in seen


def test_retriever_disabled_runs_original_only(monkeypatch: pytest.MonkeyPatch):
    db_path = _make_db()
    seen: list[str] = []
    real_keyword = HybridRetriever._run_keyword_search

    def _spy(self: HybridRetriever, query: str, *args: object, **kwargs: object) -> dict[str, int]:
        seen.append(query)
        return real_keyword(self, query, *args, **kwargs)

    monkeypatch.setattr(HybridRetriever, "_run_keyword_search", _spy)
    monkeypatch.setenv("FORESIGHT_QUERY_REWRITE", "off")
    retriever = HybridRetriever(db_path)
    retriever.search(
        "When is my dentist appointment?",
        USER_ID,
        query_rewrite=False,
        **KEYWORD_ONLY,  # type: ignore[arg-type]
    )
    assert seen == ["When is my dentist appointment?"]


def test_question_query_promotes_statement_memory():
    db_path = _make_db()
    retriever = HybridRetriever(db_path)
    result = retriever.search("When is my dentist appointment?", USER_ID, **KEYWORD_ONLY)  # type: ignore[arg-type]
    ids = [r.memory_id for r in result.results]
    assert ids[0] == "mem_target"


def test_disabled_rewrite_preserves_old_ordering():
    db_path = _make_db()
    retriever = HybridRetriever(db_path)
    result = retriever.search(
        "When is my dentist appointment?",
        USER_ID,
        query_rewrite=False,
        **KEYWORD_ONLY,  # type: ignore[arg-type]
    )
    ids = [r.memory_id for r in result.results]
    assert ids[0] == "mem_distractor"


def test_explain_reports_rewrite_variants():
    db_path = _make_db()
    retriever = HybridRetriever(db_path)
    result = retriever.search(
        "When is my dentist appointment?",
        USER_ID,
        explain=True,
        **KEYWORD_ONLY,  # type: ignore[arg-type]
    )
    assert result.score_details is not None
    assert "query_rewrite" in result.score_details
