"""Tests for evidence-linked observations (Phase 3 derived layer)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest

from foresight.observations import list_observations
from foresight.server import USER_ID, curate_observations as curate_tool, inject_context, manage_memories
from foresight.tenant_context import reset_tenant_context, set_current_account_id, set_current_user_id

TENANT = "_test_"


@pytest.fixture(autouse=True)
def setup_test_ctx():
    set_current_user_id("_test_user_")
    set_current_account_id(TENANT)
    yield
    reset_tenant_context()


def _store(content: str, category: str = "fact") -> str:
    result = manage_memories(action="store", content=content, category=category)
    match = re.search(r"Stored memory (\S+)\.", result)
    assert match, result
    return match.group(1)


def _curate(**kwargs) -> dict:
    return json.loads(curate_tool(**kwargs))


def test_curation_creates_observation_with_supporting_ids():
    stamp = datetime.now(timezone.utc).isoformat()
    _store(f"sleep quality has been poor this week ({stamp})", category="health")
    _store(f"sleep has been fragmented again last night ({stamp})", category="health")

    result = _curate()
    assert result["ok"] is True
    assert result["observations_created"] >= 1

    obs = result["observations"][0]
    assert len(obs["supporting_memory_ids"]) >= 2
    assert obs["evidence_count"] >= 2
    assert obs["version"] == 1
    assert obs["status"] == "active"
    assert obs["observation_text"]


def test_followup_curation_refines_without_dropping_evidence():
    stamp = datetime.now(timezone.utc).isoformat()
    ids = [
        _store(f"therapy session went well on monday ({stamp})", category="therapy"),
        _store(f"therapy homework completed again ({stamp})", category="therapy"),
    ]

    first = _curate()
    assert first["observations_created"] >= 1
    obs1 = next(o for o in first["observations"] if o["category"] == "therapy")
    assert set(obs1["supporting_memory_ids"]) == set(ids)

    # New evidence arrives; a follow-up run must refine, not overwrite.
    new_id = _store(f"therapy insights shared again this week ({stamp})", category="therapy")
    second = _curate()
    assert second["observations_refined"] >= 1

    refined = next(o for o in second["observations"] if o["id"] == obs1["id"])
    assert refined["version"] == 2
    assert refined["status"] == "refined"
    assert refined["last_refined_at"]
    assert set(refined["supporting_memory_ids"]) == set(ids) | {new_id}
    assert refined["evidence_count"] == 3


def test_inject_context_surfaces_patterns_section():
    stamp = datetime.now(timezone.utc).isoformat()
    _store(f"deadline pressure keeps building up ({stamp})", category="work")
    _store(f"work deadline stress recurring daily ({stamp})", category="work")
    _curate()

    output = inject_context(conversation_text="how is work going", user_id=USER_ID)
    assert "[Patterns]" in output
    assert "Recurring pattern" in output
    assert "evidence:" in output


def test_observations_are_tenant_scoped():
    stamp = datetime.now(timezone.utc).isoformat()
    _store(f"private pattern alpha ({stamp})", category="secret")
    _store(f"private pattern beta ({stamp})", category="secret")
    _curate()

    mine = list_observations(user_id=USER_ID, tenant_id=TENANT)
    assert mine

    other = list_observations(user_id="_other_user_", tenant_id=TENANT)
    assert other == []

    other_tenant = list_observations(user_id=USER_ID, tenant_id="other-tenant")
    assert other_tenant == []


def test_category_filter_scopes_scan():
    stamp = datetime.now(timezone.utc).isoformat()
    _store(f"alpha one ({stamp})", category="alpha")
    _store(f"alpha two ({stamp})", category="alpha")
    _store(f"beta one ({stamp})", category="beta")
    _store(f"beta two ({stamp})", category="beta")

    result = _curate(category="alpha")
    cats = {o["category"] for o in result["observations"]}
    assert cats == {"alpha"}
    assert result["memories_scanned"] == 2


def test_budgeted_injection_keeps_patterns_in_blocks_lane():
    stamp = datetime.now(timezone.utc).isoformat()
    _store(f"budget pattern one ({stamp})", category="budgetcat")
    _store(f"budget pattern two ({stamp})", category="budgetcat")
    _curate()

    output = inject_context(conversation_text="budget pattern", user_id=USER_ID, max_chars=4000)
    assert "pattern:" in output
