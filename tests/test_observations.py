"""Tests for evidence-linked observations (Phase 3 derived layer)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from foresight import observations as observations_module
from foresight.observations import list_observations
from foresight.reflect import UserScope
from foresight.server import (
    USER_ID,
    InjectContextParams,
    ManageMemoriesParams,
    curate_observations as curate_tool,
    inject_context,
    manage_memories,
)
from foresight.tenant_context import reset_tenant_context, set_current_account_id, set_current_user_id

TENANT = "_test_"


@pytest.fixture(autouse=True)
def setup_test_ctx():
    set_current_user_id("_test_user_")
    set_current_account_id(TENANT)
    yield
    reset_tenant_context()


def _store(content: str, category: str = "fact") -> str:
    result = manage_memories(ManageMemoriesParams(action="store", content=content, category=category))
    match = re.search(r"Stored memory (\S+)\.", result)
    assert match, result
    return match.group(1)


def _curate(**kwargs) -> dict:
    return json.loads(curate_tool(**kwargs))


def test_curation_creates_observation_with_supporting_ids():
    stamp = datetime.now(UTC).isoformat()
    seeded = [
        _store(f"sleep quality has been poor this week ({stamp})", category="health"),
        _store(f"sleep has been fragmented again last night ({stamp})", category="health"),
    ]

    result = _curate()
    assert result["ok"] is True
    assert result["observations_created"] >= 1

    obs = result["observations"][0]
    assert len(obs["supporting_memory_ids"]) >= len(seeded)
    assert obs["evidence_count"] >= len(seeded)
    assert obs["version"] == 1
    assert obs["status"] == "active"
    assert obs["observation_text"]


def test_followup_curation_refines_without_dropping_evidence():
    stamp = datetime.now(UTC).isoformat()
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
    assert refined["version"] == obs1["version"] + 1
    assert refined["status"] == "refined"
    assert refined["last_refined_at"]
    assert set(refined["supporting_memory_ids"]) == set(ids) | {new_id}
    assert refined["evidence_count"] == len(set(ids) | {new_id})


def test_inject_context_surfaces_patterns_section():
    stamp = datetime.now(UTC).isoformat()
    _store(f"deadline pressure keeps building up ({stamp})", category="work")
    _store(f"work deadline stress recurring daily ({stamp})", category="work")
    _curate()

    output = inject_context(InjectContextParams(conversation_text="how is work going", user_id=USER_ID))
    assert "[Patterns]" in output
    assert "Recurring pattern" in output
    assert "evidence:" in output


def test_observations_are_tenant_scoped():
    stamp = datetime.now(UTC).isoformat()
    _store(f"private pattern alpha ({stamp})", category="secret")
    _store(f"private pattern beta ({stamp})", category="secret")
    _curate()

    mine = list_observations(UserScope(USER_ID, TENANT))
    assert mine

    other = list_observations(UserScope("_other_user_", TENANT))
    assert other == []

    other_tenant = list_observations(UserScope(USER_ID, "other-tenant"))
    assert other_tenant == []


def test_category_filter_scopes_scan():
    stamp = datetime.now(UTC).isoformat()
    alpha_memories = [
        _store(f"alpha one ({stamp})", category="alpha"),
        _store(f"alpha two ({stamp})", category="alpha"),
    ]
    _store(f"beta one ({stamp})", category="beta")
    _store(f"beta two ({stamp})", category="beta")

    result = _curate(category="alpha")
    cats = {o["category"] for o in result["observations"]}
    assert cats == {"alpha"}
    assert result["memories_scanned"] == len(alpha_memories)


def test_budgeted_injection_keeps_patterns_in_blocks_lane():
    stamp = datetime.now(UTC).isoformat()
    _store(f"budget pattern one ({stamp})", category="budgetcat")
    _store(f"budget pattern two ({stamp})", category="budgetcat")
    _curate()

    output = inject_context(InjectContextParams(conversation_text="budget pattern", user_id=USER_ID, max_chars=4000))
    assert "pattern:" in output


def _refine_candidate(pattern_key: str, memory_ids: list[str], text: str) -> dict:
    return {
        "pattern_key": pattern_key,
        "observation_text": text,
        "category": "procedural",
        "confidence": 0.6,
        "supporting_memory_ids": memory_ids,
        "supporting_episode_ids": [],
        "memory_type": "procedural",
    }


def test_refinement_never_resurrects_archived_observation():
    stamp = datetime.now(UTC).isoformat()
    first = _store(f"archive guard step one ({stamp})", category="procedural")
    second = _store(f"archive guard step two ({stamp})", category="procedural")
    created = observations_module._upsert_observation(
        _refine_candidate("procedural:archive-guard", [first], "Run the migration smoke check."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    assert observations_module.archive_observation(created["id"], user_id=USER_ID, tenant_id=TENANT) is True

    merged = observations_module._upsert_observation(
        _refine_candidate("procedural:archive-guard", [first, second], "Run the migration smoke check twice."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    # Refining a pattern whose live row was archived opens a new observation;
    # the archived row keeps the evidence and version it was archived with.
    assert merged["id"] != created["id"]
    assert merged["status"] != "archived"

    archived = {o["id"]: o for o in list_observations(UserScope(USER_ID, TENANT), status="archived")}
    assert archived[created["id"]]["supporting_memory_ids"] == [first]
    assert archived[created["id"]]["version"] == created["version"]
    assert archived[created["id"]]["observation_text"] == created["observation_text"]


class _InterleavingConnection:
    """Connection proxy that lets a competing writer commit before our first UPDATE."""

    def __init__(self, inner, on_first_update):
        self._inner = inner
        self._on_first_update = on_first_update

    def execute(self, sql, params=()):
        if "UPDATE observations" in sql:
            fire, self._on_first_update = self._on_first_update, None
            if fire is not None:
                fire()
        return self._inner.execute(sql, params)

    def commit(self):
        return self._inner.commit()

    def close(self):
        return self._inner.close()


def test_replace_keeps_evidence_a_competing_writer_added(monkeypatch):
    stamp = datetime.now(UTC).isoformat()
    first = _store(f"race guard step one ({stamp})", category="procedural")
    pattern = "procedural:evidence-race"
    created = observations_module._upsert_observation(
        _refine_candidate(pattern, [first], "Run the migration smoke check."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    real_connect = observations_module._connect
    fired = []

    def competing_write():
        fired.append(True)
        other = real_connect()
        try:
            other.execute(
                "UPDATE observations SET supporting_episode_ids = ?, version = version + 1 WHERE id = ?",
                (json.dumps(["episode-from-other-writer"]), created["id"]),
            )
            other.commit()
        finally:
            other.close()

    def racing_connect():
        return _InterleavingConnection(real_connect(), competing_write if not fired else None)

    monkeypatch.setattr(observations_module, "_connect", racing_connect)

    merged = observations_module._upsert_observation(
        {
            **_refine_candidate(pattern, [first], "Run the migration smoke check twice."),
            "supporting_episode_ids": ["episode-this-writer-scanned"],
            "replace_episode_evidence": True,
        },
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    assert fired, "the competing writer never interleaved before our update"
    # The replacement scan never judged the other writer's episode, so the lost
    # version race must merge it rather than overwrite it.
    assert "episode-from-other-writer" in merged["supporting_episode_ids"]
    assert "episode-this-writer-scanned" in merged["supporting_episode_ids"]
    assert merged["version"] == created["version"] + 2


def test_repeated_refinement_keeps_version_when_nothing_changed():
    stamp = datetime.now(UTC).isoformat()
    first = _store(f"unchanged cluster step one ({stamp})", category="procedural")
    candidate = _refine_candidate("procedural:unchanged-cluster", [first], "Run the nightly smoke check.")
    created = observations_module._upsert_observation(candidate, user_id=USER_ID, tenant_id=TENANT)

    repeat = observations_module._upsert_observation(candidate, user_id=USER_ID, tenant_id=TENANT)
    assert repeat["version"] == created["version"]
    assert repeat["last_refined_at"] == created["last_refined_at"]

    changed = observations_module._upsert_observation(
        _refine_candidate("procedural:unchanged-cluster", [first], "Run the nightly smoke check twice."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    assert changed["version"] == created["version"] + 1
    assert changed["observation_text"] == "Run the nightly smoke check twice."


def test_confidence_only_refinement_bumps_version():
    stamp = datetime.now(UTC).isoformat()
    first = _store(f"confidence bump step one ({stamp})", category="procedural")
    created = observations_module._upsert_observation(
        _refine_candidate("procedural:confidence-bump", [first], "Run the nightly smoke check."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    assert created["confidence"] == pytest.approx(0.6)

    stronger = _refine_candidate("procedural:confidence-bump", [first], "Run the nightly smoke check.")
    stronger["confidence"] = 0.9
    refined = observations_module._upsert_observation(stronger, user_id=USER_ID, tenant_id=TENANT)

    assert refined["version"] == created["version"] + 1
    assert refined["confidence"] == pytest.approx(0.9)


def test_replacement_keeps_evidence_recorded_after_the_scan_began(monkeypatch):
    created = observations_module._upsert_observation(
        _refine_candidate("procedural:stale-replacement", [], "Rotate the key before redeploying."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    stale_scan = dict(created, supporting_episode_ids=["ep_old"])
    observations_module._upsert_observation(
        {
            "pattern_key": "procedural:stale-replacement",
            "observation_text": "Rotate the key before redeploying.",
            "category": "procedural",
            "confidence": 0.6,
            "supporting_memory_ids": [],
            "supporting_episode_ids": ["ep_old", "ep_new"],
            "memory_type": "procedural",
        },
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    monkeypatch.setattr(observations_module, "_find_observation", lambda *_a, **_k: dict(stale_scan))

    replaced = observations_module._upsert_observation(
        {
            "pattern_key": "procedural:stale-replacement",
            "observation_text": "Rotate the key, redeploy, then confirm the new key serves traffic.",
            "category": "procedural",
            "confidence": 0.6,
            "supporting_memory_ids": [],
            "supporting_episode_ids": ["ep_old"],
            "memory_type": "procedural",
            "replace_episode_evidence": True,
        },
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    assert replaced["supporting_episode_ids"] == ["ep_old", "ep_new"]
    assert replaced["version"] == created["version"] + 2


def test_confidence_only_refinement_bumps_the_version_for_rule_graduation():
    """A confidence-only improvement is a refinement, so graduation re-offers it."""
    stamp = datetime.now(UTC).isoformat()
    memory_id = _store(f"confidence guard step ({stamp})", category="procedural")
    pattern = "procedural:confidence-refine"
    created = observations_module._upsert_observation(
        {**_refine_candidate(pattern, [memory_id], "Run the migration smoke check."), "confidence": 0.4},
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    refined = observations_module._upsert_observation(
        {**_refine_candidate(pattern, [memory_id], "Run the migration smoke check."), "confidence": 0.9},
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    # Same text and same evidence, but the version must move: rule graduation
    # keys its review on the version and would never re-offer a frozen row.
    assert refined["id"] == created["id"]
    assert refined["version"] == created["version"] + 1
    assert refined["confidence"] > created["confidence"]
