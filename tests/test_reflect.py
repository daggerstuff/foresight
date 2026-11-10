"""Tests for the reflect tool — memory-backed answers with citations."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from foresight.server import manage_memories, reflect
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
