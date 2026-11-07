"""Tests for LLM entity extraction response handling."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from foresight import entity_extractor
from foresight.entity_extractor import EntityExtractor

_TRUNCATED_RESPONSE = """{
  "entities": [
    {"name": "Chad", "type": "person", "description": "Mentioned in the text", "properties": {}},
    {"name": "Dayton", "type": "place", "description": "Location", "properties": {}}
  ],
  "relationships": [
    {"source": "Chad", "target": "Dayton", "type": "located_at", "confidence": 0.9},
    {"source": "Chad", "target": "Vivi", "type": "mentions", "confidence": 0.5}
"""

_UNSALVAGEABLE_RESPONSE = '{"entities": [{"name": "Chad"'


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    def __init__(self, payload: dict, record: dict) -> None:
        self._payload = payload
        self._record = record

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self._record["url"] = url
        self._record["json"] = kwargs.get("json")
        return _FakeResponse(self._payload)


def _patch_http(monkeypatch: pytest.MonkeyPatch, content: str, finish_reason: str = "stop") -> dict:
    record: dict = {}
    payload = {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
    }
    monkeypatch.setattr(
        entity_extractor,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda timeout: _FakeAsyncClient(payload, record),
            HTTPStatusError=httpx.HTTPStatusError,
        ),
    )
    return record


def _extractor(**kwargs: object) -> EntityExtractor:
    return EntityExtractor(
        api_key="test-key",
        model="test-model",
        provider="openai",
        base_url="https://example.test/v1",
        **kwargs,
    )


def test_salvage_keeps_complete_elements_of_truncated_json() -> None:
    parsed = EntityExtractor._salvage_json(_TRUNCATED_RESPONSE)

    assert parsed is not None
    assert [e["name"] for e in parsed["entities"]] == ["Chad", "Dayton"]
    assert [r["source"] for r in parsed["relationships"]] == ["Chad", "Chad"]


def test_salvage_returns_none_when_nothing_complete() -> None:
    assert EntityExtractor._salvage_json(_UNSALVAGEABLE_RESPONSE) is None
    assert EntityExtractor._salvage_json("not json at all") is None


def test_parse_llm_response_accepts_truncated_payload() -> None:
    result = _extractor()._parse_llm_response(_TRUNCATED_RESPONSE, "Chad met Vivi in Dayton.")

    assert [e.name for e in result.entities] == ["Chad", "Dayton"]
    assert len(result.relationships) == 2
    assert result.relationships[0].relationship_type == "located_at"


def test_parse_llm_response_still_raises_on_garbage() -> None:
    with pytest.raises(json.JSONDecodeError, match="Expecting"):
        _extractor()._parse_llm_response(_UNSALVAGEABLE_RESPONSE, "Chad")


def test_default_max_tokens_leaves_room_for_full_response() -> None:
    assert EntityExtractor(api_key="test-key").max_tokens >= 2048


def test_llm_timeout_allows_for_reasoning_models() -> None:
    # Reasoning models take 50-115s; a 60s read timeout degrades to rule-based
    # extraction, which looks like a silent correctness regression.
    assert entity_extractor._LLM_TIMEOUT_SECONDS >= 120.0


@pytest.mark.asyncio
async def test_extraction_requests_configured_token_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _patch_http(monkeypatch, _TRUNCATED_RESPONSE)
    extractor = _extractor(max_tokens=4096)

    await extractor.extract_with_llm("Chad met Vivi in Dayton.")

    assert record["json"]["max_tokens"] == 4096
    assert record["url"] == "https://example.test/v1/chat/completions"


@pytest.mark.asyncio
async def test_truncated_extraction_returns_llm_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_http(monkeypatch, _TRUNCATED_RESPONSE, finish_reason="length")

    result = await _extractor().extract_with_llm("Chad met Vivi in Dayton.")

    assert [e.name for e in result.entities] == ["Chad", "Dayton"]


@pytest.mark.asyncio
async def test_unsalvageable_response_falls_back_to_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_http(monkeypatch, _UNSALVAGEABLE_RESPONSE, finish_reason="length")

    result = await _extractor().extract_with_llm("anxiety and therapy")

    assert result.entities
    assert {e.name for e in result.entities} == {"anxiety", "therapy"}
