"""Tests for evidence-linked observations (Phase 3 derived layer)."""

from __future__ import annotations

import json
import re
import uuid
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
    created, _ = observations_module._upsert_observation(
        _refine_candidate("procedural:archive-guard", [first], "Run the migration smoke check."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    assert observations_module.archive_observation(created["id"], user_id=USER_ID, tenant_id=TENANT) is True

    merged, _ = observations_module._upsert_observation(
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
    created, _ = observations_module._upsert_observation(
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

    merged, _ = observations_module._upsert_observation(
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


class _AlwaysRacingConnection:
    """Connection proxy where a competing writer wins before every UPDATE."""

    def __init__(self, inner, on_each_update):
        self._inner = inner
        self._on_each_update = on_each_update

    def execute(self, sql, params=()):
        if "UPDATE observations" in sql:
            self._on_each_update()
        return self._inner.execute(sql, params)

    def commit(self):
        return self._inner.commit()

    def close(self):
        return self._inner.close()


def test_upsert_defers_to_the_stored_row_when_every_attempt_loses(monkeypatch):
    """Five lost version races report the stored row instead of our own write."""
    stamp = datetime.now(UTC).isoformat()
    first = _store(f"exhausted race step one ({stamp})", category="procedural")
    pattern = f"procedural:exhausted-race-{uuid.uuid4().hex[:8]}"
    created, _ = observations_module._upsert_observation(
        _refine_candidate(pattern, [first], "Rebuild the search index before the backfill."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    real_connect = observations_module._connect
    losses: list[int] = []

    def competing_write():
        losses.append(len(losses) + 1)
        other = real_connect()
        try:
            other.execute(
                "UPDATE observations SET supporting_episode_ids = ?, version = version + 1 WHERE id = ?",
                (json.dumps([f"episode-other-{len(losses)}"]), created["id"]),
            )
            other.commit()
        finally:
            other.close()

    monkeypatch.setattr(
        observations_module,
        "_connect",
        lambda: _AlwaysRacingConnection(real_connect(), competing_write),
    )

    stored, _ = observations_module._upsert_observation(
        {
            **_refine_candidate(pattern, [first], "Rebuild the search index before the backfill, twice."),
            "supporting_episode_ids": ["episode-this-writer-scanned"],
            "replace_episode_evidence": True,
        },
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    merge_attempts = 5
    # The bounded loop stops after its last attempt and reports what is stored,
    # so the next pass retries instead of a write that never landed looking done.
    assert len(losses) == merge_attempts
    assert stored["observation_text"] == created["observation_text"]
    assert stored["version"] == created["version"] + merge_attempts


def test_repeated_refinement_keeps_version_when_nothing_changed():
    stamp = datetime.now(UTC).isoformat()
    first = _store(f"unchanged cluster step one ({stamp})", category="procedural")
    candidate = _refine_candidate("procedural:unchanged-cluster", [first], "Run the nightly smoke check.")
    created, _ = observations_module._upsert_observation(candidate, user_id=USER_ID, tenant_id=TENANT)

    repeat, _ = observations_module._upsert_observation(candidate, user_id=USER_ID, tenant_id=TENANT)
    assert repeat["version"] == created["version"]
    assert repeat["last_refined_at"] == created["last_refined_at"]

    changed, _ = observations_module._upsert_observation(
        _refine_candidate("procedural:unchanged-cluster", [first], "Run the nightly smoke check twice."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    assert changed["version"] == created["version"] + 1
    assert changed["observation_text"] == "Run the nightly smoke check twice."


def test_confidence_only_refinement_bumps_version():
    stamp = datetime.now(UTC).isoformat()
    first = _store(f"confidence bump step one ({stamp})", category="procedural")
    created, _ = observations_module._upsert_observation(
        _refine_candidate("procedural:confidence-bump", [first], "Run the nightly smoke check."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    assert created["confidence"] == pytest.approx(0.6)

    stronger = _refine_candidate("procedural:confidence-bump", [first], "Run the nightly smoke check.")
    stronger["confidence"] = 0.9
    refined, _ = observations_module._upsert_observation(stronger, user_id=USER_ID, tenant_id=TENANT)

    assert refined["version"] == created["version"] + 1
    assert refined["confidence"] == pytest.approx(0.9)


def test_replacement_keeps_evidence_recorded_after_the_scan_began(monkeypatch):
    created, _ = observations_module._upsert_observation(
        _refine_candidate("procedural:stale-replacement", [], "Rotate the key before redeploying."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    stale_scan = dict(created, supporting_episode_ids=["ep_old"])
    _, _ = observations_module._upsert_observation(
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

    replaced, _ = observations_module._upsert_observation(
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
    created, _ = observations_module._upsert_observation(
        {**_refine_candidate(pattern, [memory_id], "Run the migration smoke check."), "confidence": 0.4},
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    refined, _ = observations_module._upsert_observation(
        {**_refine_candidate(pattern, [memory_id], "Run the migration smoke check."), "confidence": 0.9},
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    # Same text and same evidence, but the version must move: rule graduation
    # keys its review on the version and would never re-offer a frozen row.
    assert refined["id"] == created["id"]
    assert refined["version"] == created["version"] + 1
    assert refined["confidence"] > created["confidence"]


def test_replace_with_a_stale_scan_version_is_refused():
    stamp = datetime.now(UTC).isoformat()
    memory_id = _store(f"stale version guard ({stamp})", category="procedural")
    pattern = "procedural:stale-version"
    created, _ = observations_module._upsert_observation(
        _refine_candidate(pattern, [memory_id], "Rotate the key before redeploying."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    newer, _ = observations_module._upsert_observation(
        _refine_candidate(pattern, [memory_id], "Rotate the key, then confirm the new key serves traffic."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    assert newer["version"] > created["version"]

    refused, _ = observations_module._upsert_observation(
        {
            "pattern_key": pattern,
            "observation_text": "A rewrite built from a scan that started before the last write.",
            "category": "procedural",
            "confidence": 0.6,
            "supporting_memory_ids": [],
            "supporting_episode_ids": [memory_id],
            "memory_type": "procedural",
            "replace_episode_evidence": True,
            "scan_base_version": created["version"],
        },
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    assert refused["observation_text"] == newer["observation_text"]
    assert refused["version"] == newer["version"]
    # The refusal must hand back the newer row's own evidence: a merged copy
    # would list episodes the scan never re-judged as still supporting it.
    assert refused["supporting_episode_ids"] == newer["supporting_episode_ids"]


def test_a_scan_version_matching_the_stored_row_still_writes():
    stamp = datetime.now(UTC).isoformat()
    memory_id = _store(f"matching version guard ({stamp})", category="procedural")
    pattern = "procedural:matching-version"
    created, _ = observations_module._upsert_observation(
        _refine_candidate(pattern, [memory_id], "Run the migration smoke check first."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    refined, _ = observations_module._upsert_observation(
        {
            "pattern_key": pattern,
            "observation_text": "Run the migration smoke check, then deploy.",
            "category": "procedural",
            "confidence": 0.6,
            "supporting_memory_ids": [],
            "supporting_episode_ids": [memory_id],
            "memory_type": "procedural",
            "replace_episode_evidence": True,
            "scan_base_version": created["version"],
        },
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    assert refined["observation_text"] == "Run the migration smoke check, then deploy."
    assert refined["version"] == created["version"] + 1
    assert refined["supporting_episode_ids"] == [memory_id]


def test_a_replacement_built_on_an_empty_scan_yields_to_the_row_that_appeared():
    """A scan that found nothing still orders its replacement before the row it wrote."""
    stamp = datetime.now(UTC).isoformat()
    memory_id = _store("empty scan guard (" + stamp + ")", category="procedural")
    pattern = "procedural:empty-scan"
    created, written = observations_module._upsert_observation(
        _refine_candidate(pattern, [memory_id], "Run the migration smoke check first."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    assert written is True

    refused, wrote = observations_module._upsert_observation(
        {
            "pattern_key": pattern,
            "observation_text": "A rewrite built from a scan that found no observation.",
            "category": "procedural",
            "confidence": 0.6,
            "supporting_memory_ids": [],
            "supporting_episode_ids": ["episode-scan-found-nothing"],
            "memory_type": "procedural",
            "replace_episode_evidence": True,
            "scan_base_episode_ids": (),
            "scan_base_version": 0,
        },
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    assert wrote is False
    assert refused["id"] == created["id"]
    assert refused["observation_text"] == created["observation_text"]
    assert "episode-scan-found-nothing" not in refused["supporting_episode_ids"]


def test_a_replacement_that_loses_the_update_race_defers_to_the_newer_row(monkeypatch):
    """The version guard is re-read after a lost update instead of merged over."""
    stamp = datetime.now(UTC).isoformat()
    first = _store("stale replacement step one (" + stamp + ")", category="procedural")
    second = _store("stale replacement step two (" + stamp + ")", category="procedural")
    pattern = "procedural:stale-replacement-race"
    created, _ = observations_module._upsert_observation(
        _refine_candidate(pattern, [first], "Run the migration smoke check."),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    seeded, _ = observations_module._upsert_observation(
        {
            "pattern_key": pattern,
            "observation_text": "Run the migration smoke check.",
            "category": "procedural",
            "confidence": 0.5,
            "supporting_memory_ids": [second],
            "supporting_episode_ids": ["ep-a", "ep-b"],
            "memory_type": "procedural",
        },
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    assert sorted(seeded["supporting_episode_ids"]) == ["ep-a", "ep-b"]

    real_connect = observations_module._connect
    fired = []

    def competing_write():
        fired.append(True)
        other = real_connect()
        try:
            other.execute(
                "UPDATE observations SET supporting_episode_ids = ?, version = version + 1 WHERE id = ?",
                (json.dumps(["ep-a"]), created["id"]),
            )
            other.commit()
        finally:
            other.close()

    def racing_connect():
        return _InterleavingConnection(real_connect(), competing_write if not fired else None)

    monkeypatch.setattr(observations_module, "_connect", racing_connect)
    deferred, written = observations_module._upsert_observation(
        {
            "pattern_key": pattern,
            "observation_text": "Replacement built from the older scan.",
            "category": "procedural",
            "confidence": 0.9,
            "supporting_memory_ids": [first],
            "supporting_episode_ids": ["ep-c"],
            "memory_type": "procedural",
            "replace_episode_evidence": True,
            "scan_base_episode_ids": ("ep-a", "ep-b"),
            "scan_base_version": seeded["version"],
        },
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    assert fired, "the competing writer never ran"
    assert written is False
    assert deferred["observation_text"] == "Run the migration smoke check."
    assert deferred["supporting_episode_ids"] == ["ep-a"]
    assert deferred["version"] == seeded["version"] + 1
