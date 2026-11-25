from __future__ import annotations

import json
import uuid

import pytest

from foresight.capture import CaptureOptions, get_capture_pipeline
from foresight.experience import (
    EpisodeVerdict,
    auto_score_episode,
    build_episode,
    distill_experiences,
    normalize_error_signature,
    record_outcome,
)
from foresight.hybrid_retriever import HybridSearchOptions, get_hybrid_retriever
from foresight.observations import list_observations
from foresight.reflect import UserScope, _fetch_all
from foresight.server import RecordEpisodeOutcomeParams, record_episode_outcome
from foresight.tenant_context import reset_tenant_context, set_current_account_id, set_current_user_id

TENANT = "_test_experience_"
USER = "_test_experience_user_"


@pytest.fixture(autouse=True)
def _ctx(monkeypatch):
    monkeypatch.setenv("FORESIGHT_RAW_RETENTION", "1")
    monkeypatch.setenv("FORESIGHT_EPISODE_SCORING", "fallback")
    set_current_user_id(USER)
    set_current_account_id(TENANT)
    yield
    reset_tenant_context()


def _session_id() -> str:
    return f"exp-{uuid.uuid4().hex[:12]}"


def _failure_transcript() -> list[dict]:
    return [
        {"role": "user", "content": "deploy the staging release for the billing service"},
        {"role": "assistant", "content": "running deploy now"},
        {
            "role": "tool",
            "content": 'Traceback (most recent call last):\n  File "deploy.py", line 88\n'
            "ConnectionRefusedError: connection refused on db.internal:5432",
        },
    ]


def _success_transcript(marker: str) -> list[dict]:
    return [
        {"role": "user", "content": f"run the billing service release checklist {marker}"},
        {"role": "assistant", "content": "running tests and build"},
        {"role": "tool", "content": "52 passed in 3.1s\nexit code 0"},
    ]


def _episode_for(session_id: str, messages: list[dict]):
    return build_episode(session_id, user_id=USER, tenant_id=TENANT, messages=messages)


def test_build_episode_links_raw_events():
    session_id = _session_id()
    messages = _failure_transcript()
    stats = get_capture_pipeline().run(
        session_id=session_id,
        messages=messages,
        user_id=USER,
        tenant_id=TENANT,
        options=CaptureOptions(infer=False),
    )
    assert stats is not None

    episode = build_episode(session_id, user_id=USER, tenant_id=TENANT)
    assert episode["session_id"] == session_id
    assert episode["raw_event_count"] == len(messages)
    assert episode["raw_event_first_id"]
    assert episode["raw_event_last_id"]
    assert episode["summary"].startswith("deploy the staging release")

    links = _fetch_all("SELECT raw_event_id FROM episode_events WHERE episode_id = ?", (episode["id"],))
    assert len(links) == len(messages)

    again = build_episode(session_id, user_id=USER, tenant_id=TENANT)
    assert again["id"] == episode["id"]
    assert again["raw_event_count"] == len(messages)


def test_normalize_error_signature():
    sig = normalize_error_signature("Traceback...\nConnectionRefusedError: connection refused on 10.0.0.8:5432")
    assert sig is not None
    assert "connectionrefusederror" in sig
    assert "10" not in sig
    assert normalize_error_signature("everything is fine") is None


def test_fallback_scoring_marks_failure_and_success():
    fail_session = _session_id()
    ok_session = _session_id()
    ep_fail = _episode_for(fail_session, _failure_transcript())
    ep_ok = _episode_for(ok_session, _success_transcript("scoring-ok"))

    fail_scored = auto_score_episode(ep_fail["id"], user_id=USER, tenant_id=TENANT, messages=_failure_transcript())
    ok_scored = auto_score_episode(
        ep_ok["id"], user_id=USER, tenant_id=TENANT, messages=_success_transcript("scoring-ok")
    )

    assert fail_scored["outcome"] == "failure"
    assert fail_scored["score_source"] == "fallback"
    assert "degraded" in (fail_scored["score_detail"] or "")
    assert fail_scored["error_signature"]

    assert ok_scored["outcome"] == "success"
    assert ok_scored["outcome_score"] == pytest.approx(0.9)


def test_explicit_outcome_wins_over_auto_scoring():
    session_id = _session_id()
    ep = _episode_for(session_id, _failure_transcript())
    record_outcome(
        USER,
        TENANT,
        EpisodeVerdict(
            outcome="success",
            score_source="user_feedback",
            detail="actually the deploy went out fine the next morning",
            session_id=session_id,
        ),
    )
    rescored = auto_score_episode(ep["id"], user_id=USER, tenant_id=TENANT, messages=_failure_transcript())
    assert rescored["outcome"] == "success"
    assert rescored["score_source"] == "user_feedback"


def test_ci_result_source_is_recorded():
    session_id = _session_id()
    _episode_for(session_id, _success_transcript("ci-source"))
    updated = record_outcome(
        USER,
        TENANT,
        EpisodeVerdict(
            outcome="failure",
            score_source="ci_result",
            detail="FAILED test_deploy_smoke - ConnectionRefusedError: connection refused",
            session_id=session_id,
        ),
    )
    assert updated["score_source"] == "ci_result"
    assert updated["outcome_score"] == 0.0
    assert updated["error_signature"]


def test_llm_rubric_path_and_fallback_degradation(monkeypatch):
    session_id = _session_id()
    ep = _episode_for(session_id, _failure_transcript())

    monkeypatch.setenv("FORESIGHT_EPISODE_SCORING", "llm")
    monkeypatch.setattr(
        "foresight.experience._llm_rubric", lambda *_a, **_k: (0.1, "rubric: tests failed")
    )
    scored = auto_score_episode(ep["id"], user_id=USER, tenant_id=TENANT, messages=_failure_transcript())
    assert scored["score_source"] == "llm_rubric"
    assert scored["outcome"] == "failure"

    def _boom(*_args, **_kwargs):
        raise RuntimeError("llm unavailable")

    monkeypatch.setattr("foresight.experience._llm_rubric", _boom)
    degraded = auto_score_episode(ep["id"], user_id=USER, tenant_id=TENANT, messages=_failure_transcript())
    assert degraded["score_source"] == "fallback"
    assert "degraded" in (degraded["score_detail"] or "")


def _distill_failing_episode(marker: str):
    session_id = _session_id()
    messages = _failure_transcript()
    ep = _episode_for(session_id, messages)
    record_outcome(
        USER,
        TENANT,
        EpisodeVerdict(
            outcome="failure",
            score_source="ci_result",
            detail=f"FAILED deploy smoke ({marker}) - ConnectionRefusedError: connection refused on db.internal:5432",
            session_id=session_id,
        ),
    )
    return ep


def test_distiller_creates_procedural_observation_and_memory():
    ep1 = _distill_failing_episode("distiller-a")
    ep2 = _distill_failing_episode("distiller-b")

    result = distill_experiences(user_id=USER, tenant_id=TENANT)
    assert result["failures_distilled"] >= 1

    rows = _fetch_all(
        "SELECT * FROM observations WHERE tenant_id = ? AND user_id = ? AND memory_type = 'procedural' "
        "AND pattern_key LIKE ?",
        (TENANT, USER, "failure:%"),
    )
    assert rows, "expected a procedural failure observation"
    target = next(
        r
        for r in rows
        if ep1["id"] in json.loads(r["supporting_episode_ids"] or "[]")
        and ep2["id"] in json.loads(r["supporting_episode_ids"] or "[]")
    )
    assert "connectionrefusederror" in target["pattern_key"]

    memories = _fetch_all(
        "SELECT * FROM memories WHERE tenant_id = ? AND user_id = ? AND memory_type = 'procedural' "
        "AND error_signature IS NOT NULL",
        (TENANT, USER),
    )
    assert memories, "expected a procedural memory carrying the error signature"
    assert any("connectionrefusederror" in (m["error_signature"] or "") for m in memories)


def test_distiller_skips_sensitive_episodes():
    session_id = _session_id()
    sensitive = [
        {"role": "user", "content": "patient diagnosis: prescription dosage adjustment needed"},
        {"role": "assistant", "content": "noted the medication and dosage for the patient"},
        {"role": "tool", "content": "Traceback (most recent call last):\nConnectionRefusedError: connection refused"},
    ]
    ep = _episode_for(session_id, sensitive)
    assert ep["is_sensitive"] == 1
    record_outcome(
        USER,
        TENANT,
        EpisodeVerdict(
            outcome="failure",
            score_source="ci_result",
            detail="ConnectionRefusedError: connection refused",
            session_id=session_id,
        ),
    )
    distill_experiences(user_id=USER, tenant_id=TENANT)
    sensitive_obs = _fetch_all(
        "SELECT * FROM observations WHERE tenant_id = ? AND user_id = ? AND supporting_episode_ids LIKE ?",
        (TENANT, USER, f"%{ep['id']}%"),
    )
    assert sensitive_obs == []


def test_success_episode_becomes_recipe_observation():
    session_id = _session_id()
    _episode_for(session_id, _success_transcript("recipe-check"))
    record_outcome(
        USER,
        TENANT,
        EpisodeVerdict(outcome="success", score_source="ci_result", session_id=session_id),
    )
    distill_experiences(user_id=USER, tenant_id=TENANT)
    obs = list_observations(UserScope(user_id=USER, tenant_id=TENANT), status="active", limit=50)
    recipes = [o for o in obs if o["pattern_key"].startswith("success:") and o["memory_type"] == "procedural"]
    assert recipes, "expected a procedural success observation"


def test_keyword_search_surfaces_failure_signature():
    _distill_failing_episode("recall-check")
    distill_experiences(user_id=USER, tenant_id=TENANT)

    retriever = get_hybrid_retriever()
    result = retriever.search(
        "ConnectionRefusedError connection refused during deploy",
        USER,
        HybridSearchOptions(tenant_id=TENANT, limit=10),
    )
    found = {hit.memory_id for hit in result.results}
    rows = _fetch_all(
        "SELECT id FROM memories WHERE tenant_id = ? AND user_id = ? AND memory_type = 'procedural' "
        "AND error_signature LIKE ?",
        (TENANT, USER, "%connectionrefusederror%"),
    )
    expected_ids = {r["id"] for r in rows}
    assert expected_ids, "expected distilled failure memory to exist"
    assert found & expected_ids, "keyword retrieval did not surface the distilled failure memory"

    # Volatile tokens (host/port/line) differ from the stored failure, yet the
    # normalized error_signature index still surfaces the prior failure lesson.
    variant_result = retriever.search(
        "File /tmp/run99/app.py line 412 ConnectionRefusedError: connection refused on 10.20.30.40:9999",
        USER,
        HybridSearchOptions(tenant_id=TENANT, limit=10),
    )
    variant_found = {hit.memory_id for hit in variant_result.results}
    assert variant_found & expected_ids, "normalized error_signature index did not match across volatile tokens"


def test_record_outcome_tool_roundtrip():
    session_id = _session_id()
    _episode_for(session_id, _success_transcript("tool-roundtrip"))
    raw = record_episode_outcome(
        RecordEpisodeOutcomeParams(
            session_id=session_id,
            outcome="failure",
            score_source="ci_result",
            detail="FAILED deploy - ConnectionRefusedError: connection refused",
        )
    )
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["episode"]["outcome"] == "failure"
    assert payload["episode"]["score_source"] == "ci_result"
