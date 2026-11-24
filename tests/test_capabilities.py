"""Tests for capability report and explicit degraded flags (FOR-18 / PIX-448)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from foresight.capabilities import get_capabilities_report
from foresight.entity_extractor import EntityExtractor
from foresight.reflect import UserScope, reflect
from foresight.server import (
    GetSystemStatusParams,
    ManageMemoriesParams,
    MemoryAction,
    MemoryOptions,
    get_system_status,
    manage_memories,
)
from foresight_cli.commands.system import _doctor_check_capabilities, _DoctorReport

MIN_EXPECTED_CHECKS = 5


def test_get_capabilities_report_structure() -> None:
    """Capabilities report returns all 5 required subsystem blocks."""
    caps = get_capabilities_report()
    assert "embedder" in caps
    assert "reranker" in caps
    assert "llm" in caps
    assert "encryption" in caps
    assert "redis" in caps

    # Embedder
    assert "provider" in caps["embedder"]
    assert "real" in caps["embedder"]
    assert "dimension" in caps["embedder"]
    assert isinstance(caps["embedder"]["real"], bool)

    # Reranker
    assert "enabled" in caps["reranker"]
    assert "active" in caps["reranker"]

    # LLM
    assert "configured" in caps["llm"]

    # Encryption
    assert "enabled" in caps["encryption"]
    assert "key_present" in caps["encryption"]

    # Redis
    assert "configured" in caps["redis"]
    assert "connected" in caps["redis"]


def test_system_status_includes_capabilities(reset_test_tables) -> None:
    """get_system_status JSON includes the capabilities block."""
    _ = reset_test_tables
    res_str = get_system_status(GetSystemStatusParams(user_id="test_user"))
    data = json.loads(res_str)
    assert "capabilities" in data
    assert data["capabilities"]["embedder"]["provider"] is not None
    assert "reranker" in data["capabilities"]
    assert "llm" in data["capabilities"]
    assert "encryption" in data["capabilities"]
    assert "redis" in data["capabilities"]


def test_doctor_check_capabilities_runs() -> None:
    """CLI doctor checks capabilities successfully."""
    report = _DoctorReport()
    caps = _doctor_check_capabilities(report)
    assert caps is not None
    assert report.passed >= MIN_EXPECTED_CHECKS


def test_reflect_extractive_fallback_sets_degraded(reset_test_tables) -> None:
    """When LLM is not configured, reflect produces degraded=True with reason."""
    _ = reset_test_tables
    scope = UserScope(user_id="test_user", tenant_id="tenant_test")
    manage_memories(
        ManageMemoriesParams(
            options=MemoryAction(
                action="store",
                content="Patient prefers morning therapy appointments.",
                options=MemoryOptions(category="preference"),
            ),
            user_id="test_user",
        )
    )

    with patch.dict("os.environ", {"FORESIGHT_LLM_PROVIDER": "none", "FORESIGHT_LLM_API_KEY": ""}):
        result = reflect("When does the patient prefer appointments?", scope)
        assert result.get("synthesized_by") == "extractive"
        assert result.get("degraded") is True
        assert "extractive reflect fallback" in result.get("degraded_reason", "")


@pytest.mark.asyncio
async def test_entity_extractor_rules_based_sets_degraded() -> None:
    """EntityExtractor rule-based fallback flags degraded=True."""
    extractor = EntityExtractor()
    extractor.api_key = None  # Force rules fallback
    result = await extractor.extract("I am feeling lots of anxiety and stress about therapy.")
    assert result.degraded is True
    assert "rule-based extraction fallback" in (result.degraded_reason or "")
    dict_res = result.to_dict()
    assert dict_res["degraded"] is True
    assert "rule-based extraction fallback" in dict_res["degraded_reason"]
