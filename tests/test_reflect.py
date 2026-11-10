"""Tests for the reflect tool — memory-backed answers with citations."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from foresight.server import USER_ID, manage_memories, reflect
from foresight.tenant_context import reset_tenant_context, set_current_account_id, set_current_user_id


@pytest.fixture(autouse=True)
def setup_test_ctx():
    set_current_user_id("_test_user_")
    set_current_account_id("_test_")
    yield
    reset_tenant_context()


def _store(content: str, **kwargs) -> str:
    import re

    result = manage_memories(action="store", content=content, category="fact", **kwargs)
    match = re.search(r"Stored memory (\S+)\.", result)
    assert match, result
    return match.group(1)


def _reflect(question: str, **kwargs) -> dict:
    return json.loads(reflect(question=question, **kwargs))


def test_reflect_returns_cited_answer():
    content = f"The user's favorite editor is neovim ({datetime.now(timezone.utc).isoformat()})"
    memory_id = _store(content)

    payload = _reflect("What is the user's favorite editor?")

    assert payload["answer"]
    assert payload["cached"] is False
    assert payload["tokens_used"] > 0
    citation_ids = {c["memory_id"] for c in payload["citations"]}
    assert memory_id in citation_ids
    for c in payload["citations"]:
        assert c["memory_id"]
        assert c["content_excerpt"]
        assert c["score"] >= 0
        assert c["why_relevant"]


def test_reflect_extractive_fallback_without_llm(monkeypatch):
    """No LLM configured (or failing) → deterministic extractive answer, never fails."""
    from foresight import llm_client

    def _boom(*args, **kwargs):
        raise llm_client.LLMError("no provider")

    monkeypatch.setattr(llm_client, "default_llm_call", _boom)

    _store(f"Foresight stores derived memories in postgres ({datetime.now(timezone.utc).isoformat()})")
    payload = _reflect("Where are derived memories stored?")

    assert payload["synthesized_by"] == "extractive"
    assert "memor" in payload["answer"].lower()


def test_reflect_no_memories_is_graceful():
    payload = _reflect("What is the airspeed velocity of an unladen swallow?")
    assert payload["answer"]
    assert payload["citations"] == []
    assert payload["memories_used"] == 0


def test_reflect_tenant_isolation():
    _store(f"secret project falcon plans ({datetime.now(timezone.utc).isoformat()})")

    # Same question under a different user sees nothing.
    other = json.loads(reflect(question="secret project falcon plans", user_id="_other_user_"))
    assert other["citations"] == []


def test_reflect_include_archived_flag_accepted():
    _store(f"archived candidate memory ({datetime.now(timezone.utc).isoformat()})")
    payload = _reflect("archived candidate memory", include_archived=True)
    assert payload["answer"]


def test_reflect_records_telemetry(monkeypatch, tmp_path):
    store_path = tmp_path / "telemetry.json"
    monkeypatch.setenv("FORESIGHT_TELEMETRY_PATH", str(store_path))
    from foresight import telemetry

    telemetry._GLOBAL_TELEMETRY_STORE = telemetry.TelemetryStore(store_path)
    try:
        _store(f"telemetry probe memory ({datetime.now(timezone.utc).isoformat()})")
        _reflect("telemetry probe memory")
        summary = telemetry.get_telemetry_store().get_summary()
        assert summary["reflect_calls"] >= 1
        assert summary["reflect_cache_misses"] >= 1
    finally:
        telemetry._GLOBAL_TELEMETRY_STORE = None


# ====== Phase 2: mental models ======


def test_reflect_second_identical_question_is_cached():
    _store(f"cached reflection target ({datetime.now(timezone.utc).isoformat()})")

    first = _reflect("What is the cached reflection target?")
    assert first["cached"] is False
    assert first["mental_model_id"]

    second = _reflect("what is the cached reflection target")  # case differs; key normalizes
    assert second["cached"] is True
    assert second["tokens_used"] == 0
    assert second["mental_model_id"] == first["mental_model_id"]
    assert second["answer"] == first["answer"]


def test_reflect_cache_hit_costs_zero_retrieval(monkeypatch):
    from foresight.hybrid_retriever import get_hybrid_retriever

    retriever = get_hybrid_retriever()
    _store(f"zero cost probe ({datetime.now(timezone.utc).isoformat()})")
    _reflect("zero cost probe")

    def _boom(*args, **kwargs):
        raise AssertionError("retrieval ran on a cache hit")

    monkeypatch.setattr(retriever, "search", _boom)
    payload = _reflect("zero cost probe")
    assert payload["cached"] is True


def test_mental_models_are_tenant_scoped():
    _store(f"scoped model memory ({datetime.now(timezone.utc).isoformat()})")
    _reflect("scoped model memory")

    other = _reflect("scoped model memory", user_id="_other_user_")
    assert other["cached"] is False


def test_manage_mental_models_list_refresh_delete():
    from foresight.server import manage_mental_models

    _store(f"managed model memory ({datetime.now(timezone.utc).isoformat()})")
    first = _reflect("managed model memory")
    model_id = first["mental_model_id"]

    listed = json.loads(manage_mental_models(action="list"))
    assert listed["ok"] is True
    assert any(m["id"] == model_id for m in listed["models"])

    refreshed = json.loads(manage_mental_models(action="refresh", model_id=model_id))
    assert refreshed["ok"] is True
    assert refreshed["model"]["question_key"]

    deleted = json.loads(manage_mental_models(action="delete", model_id=model_id))
    assert deleted["ok"] is True

    gone = json.loads(manage_mental_models(action="refresh", model_id=model_id))
    assert gone["ok"] is False


def test_stale_mental_model_self_heals(monkeypatch):
    from foresight import reflect as reflect_mod

    _store(f"self heal memory ({datetime.now(timezone.utc).isoformat()})")
    first = _reflect("self heal memory")
    assert first["citations"], first
    assert first["mental_model_id"]

    # Simulate supersession invalidation marking the model stale.
    reflect_mod.mark_mental_models_stale(
        [c["memory_id"] for c in first["citations"]], user_id=USER_ID, tenant_id="_test_"
    )

    second = _reflect("self heal memory")
    assert second["cached"] is False
    model = reflect_mod.lookup_mental_model(
        reflect_mod.question_key("self heal memory"), user_id=USER_ID, tenant_id="_test_"
    )
    assert model is not None
    assert model["stale"] is False


def test_supersede_invalidates_dependent_mental_model(monkeypatch):
    monkeypatch.setenv("FORESIGHT_SUPERSEDE_DETECTION", "1")
    old = _store("I go to the gym every morning before work")
    _reflect("Do I go to the gym every morning?")

    # Storing the contradicting memory triggers truth-resolution supersede,
    # which must mark dependent mental models stale.
    _store("I stopped going to the gym every morning")

    from foresight import reflect as reflect_mod

    models = reflect_mod.list_mental_models(user_id=USER_ID, tenant_id="_test_")
    assert models
    model = next(m for m in models if old in [c.get("memory_id") for c in m["citations"]])
    assert model["stale"] is True

    # Next reflect on the same question recomputes instead of serving stale receipts.
    again = _reflect("Do I go to the gym every morning?")
    assert again["cached"] is False
