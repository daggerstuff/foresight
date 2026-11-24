"""Tests for FOR-22 / PIX-449 write-time link candidate suggestions.

Covers:
- `_suggest_relation_type` heuristic classification without LLM calls.
- `_find_link_candidates` near-neighbor suggestion and scoring.
- Store response with `response_format="json"` providing a structured `candidates` field.
- Store response with `response_format="text"` including Candidates summary.
- Agents confirming candidates via `link_memories` without automatic linking.
"""

import json
from unittest.mock import patch

import pytest

from foresight.server import (
    LinkMemoriesParams,
    ManageMemoriesParams,
    MemoryOptions,
    _find_link_candidates,
    _StoreRequest,
    _suggest_relation_type,
    link_memories,
    manage_memories,
)
from foresight.tenant_context import set_current_account_id


def test_suggest_relation_type_heuristics():
    """Verify heuristic classification handles contradiction, causality, and support."""
    # Contradiction
    assert (
        _suggest_relation_type("We use Python 3.12 instead of Python 3.10", "We use Python 3.10", 0.6) == "contradicts"
    )
    assert _suggest_relation_type("The old API is deprecated", "Old API docs", 0.4) == "contradicts"

    # Causality / derivation
    assert _suggest_relation_type("Latency spiked because cache was evicted", "Cache was evicted", 0.4) == "derives"
    assert _suggest_relation_type("Deploy failed due to network timeout", "Network timeout error", 0.4) == "derives"

    # High similarity / support
    assert (
        _suggest_relation_type("We deploy Postgres 17 with pgvector", "PostgreSQL 17 is standard DB", 0.65)
        == "supports"
    )

    # Fallback
    assert _suggest_relation_type("Unrelated system note", "Some other note", 0.25) == "related"


@pytest.mark.usefixtures("reset_test_tables")
def test_write_time_link_suggestions_json_format():
    """Verify store returns candidate links in JSON format."""
    set_current_account_id("test_tenant")

    # Store first memory
    res1 = manage_memories(
        ManageMemoriesParams(
            action="store",
            content="Deploying FastAPI service with PostgreSQL database on AWS.",
            user_id="user_for22",
            response_format="json",
        )
    )
    data1 = json.loads(res1)
    assert data1["status"] == "stored"
    assert "memory_id" in data1
    assert "candidates" in data1
    assert isinstance(data1["candidates"], list)
    mem1_id = data1["memory_id"]

    # Store second related memory with causality
    res2 = manage_memories(
        ManageMemoriesParams(
            action="store",
            content="FastAPI service latency increased because database connection pool was exhausted.",
            user_id="user_for22",
            response_format="json",
        )
    )
    data2 = json.loads(res2)
    assert data2["status"] == "stored"
    assert "memory_id" in data2
    mem2_id = data2["memory_id"]
    assert mem2_id != mem1_id

    # The first memory should be suggested as candidate
    cands = data2["candidates"]
    assert len(cands) >= 1
    cand_ids = [c["memory_id"] for c in cands]
    assert mem1_id in cand_ids

    matching = next(c for c in cands if c["memory_id"] == mem1_id)
    assert matching["score"] > 0
    assert "suggested_relation" in matching
    assert "preview" in matching


@pytest.mark.usefixtures("reset_test_tables")
def test_write_time_link_suggestions_text_format():
    """Verify store returns candidate link summary in text format."""
    set_current_account_id("test_tenant")

    # Store first memory
    manage_memories(
        ManageMemoriesParams(
            action="store",
            content="Redis cluster cache configured with LRU eviction policy.",
            user_id="user_for22_text",
            response_format="text",
        )
    )

    # Store second memory
    res2 = manage_memories(
        ManageMemoriesParams(
            action="store",
            content="Cache hit ratio dropped due to Redis cluster failover.",
            user_id="user_for22_text",
            response_format="text",
        )
    )
    assert "Stored memory " in res2
    assert "Gate: " in res2
    assert "Candidates: [" in res2


@pytest.mark.usefixtures("reset_test_tables")
def test_agent_can_confirm_candidates_via_link_memories():
    """Verify that suggestions are not automatically linked until confirmed via link_memories."""
    set_current_account_id("test_tenant")

    manage_memories(
        ManageMemoriesParams(
            action="store",
            content="Authentication uses JWT tokens signed with RS256.",
            user_id="user_for22_confirm",
            response_format="json",
        )
    )

    res2 = manage_memories(
        ManageMemoriesParams(
            action="store",
            content="Auth service failed because token signature verification expired.",
            user_id="user_for22_confirm",
            response_format="json",
        )
    )
    cand = json.loads(res2)["candidates"][0]
    mem2_id = json.loads(res2)["memory_id"]

    # Confirm candidate manually with link_memories
    link_res = link_memories(
        LinkMemoriesParams(
            source_memory_id=mem2_id,
            target_memory_id=cand["memory_id"],
            relationship_type=cand["suggested_relation"],
            confidence=cand["score"],
            user_id="user_for22_confirm",
        )
    )
    link_data = json.loads(link_res)
    assert link_data["source_memory_id"] == mem2_id
    assert link_data["target_memory_id"] == cand["memory_id"]
    assert link_data["relationship_type"] == cand["suggested_relation"]


def test_find_link_candidates_resilience():
    """Verify _find_link_candidates degrades gracefully on error without throwing."""
    req = _StoreRequest(uid="u1", tenant_id="t1", content="Some text", opts=MemoryOptions())
    with patch("foresight.server.get_hybrid_retriever", side_effect=RuntimeError("Retriever down")):
        cands = _find_link_candidates(req, "m1")
        assert cands == []
