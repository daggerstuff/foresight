from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

import foresight.auth as auth_module
import foresight.experience as experience_module
from foresight.auth import (
    OAuthCallerMiddleware,
    Role,
    User,
    reset_caller_identity,
    set_caller_identity,
)
from foresight.capture import CaptureOptions, get_capture_pipeline
from foresight.experience import (
    EpisodeVerdict,
    _procedure_steps,
    auto_score_episode,
    build_episode,
    distill_experiences,
    get_episode,
    normalize_error_signature,
    record_outcome,
)
from foresight.hybrid_retriever import HybridSearchOptions, get_hybrid_retriever
from foresight.observations import list_observations
from foresight.reflect import UserScope, _execute, _fetch_all
from foresight.sensitivity import reset_secret_defense_policies, set_secret_defense_policy
from foresight.server import RecordEpisodeOutcomeParams, record_episode_outcome
from foresight.tenant_context import reset_tenant_context, set_current_account_id, set_current_user_id

TENANT = "_test_experience_"
USER = "_test_experience_user_"
# Three scored episodes fetched with limit=2 must all be scanned (pages of 2 then 1).
EXPECTED_PAGED_EPISODES = 3


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
    # A 0x-prefixed id and a bare id are the same volatile token.
    assert normalize_error_signature("RuntimeError: bad handle 0xdeadbeef") == normalize_error_signature(
        "RuntimeError: bad handle deadbeef"
    )


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
    monkeypatch.setattr("foresight.experience._llm_rubric", lambda *_a, **_k: (0.1, "rubric: tests failed"))
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

    # Volatile tokens (host/port/line) differ from the stored failure. Strip the
    # content so no word of the later query appears in the row: only the
    # normalized error_signature index can still surface the prior lesson.
    for row in rows:
        _execute(
            "UPDATE memories SET error_signature = ?, content = ? WHERE id = ?",
            (
                "connectionrefusederror: connection refused on <host>:<num>",
                "unrelated note about release choreography",
                row["id"],
            ),
        )
    ranked = retriever._keyword_search(
        "File /tmp/run99/app.py line 412 ConnectionRefusedError: connection refused on 10.20.30.40:9999",
        USER,
        TENANT,
        10,
        HybridSearchOptions(tenant_id=TENANT, limit=10),
    )

    assert expected_ids & set(ranked), "normalized error_signature index did not match across volatile tokens"


def test_recall_matches_a_signature_stored_without_detail():
    """A lesson recorded from a detail-less exception still matches its class.

    ``normalize_error_signature`` keeps ``ConnectionRefusedError:`` alone when
    the transcript carries no detail, so the stored signature is the bare class
    name. A later, detailed failure of the same class has to surface it.
    """
    _distill_failing_episode("recall-bare-class")
    distill_experiences(user_id=USER, tenant_id=TENANT)

    rows = _fetch_all(
        "SELECT id FROM memories WHERE tenant_id = ? AND user_id = ? AND memory_type = 'procedural' "
        "AND error_signature LIKE ?",
        (TENANT, USER, "connectionrefusederror%"),
    )
    assert rows, "expected a distilled failure memory"
    # Store the detail-less shape the extractor produces, and strip every other
    # matching signal: the signature index is then the only path to this row.
    for row in rows:
        _execute(
            "UPDATE memories SET error_signature = ?, content = ?, tags = '[]', metrics = '{}' WHERE id = ?",
            ("connectionrefusederror", "unrelated note about release choreography", row["id"]),
        )
    expected = {str(row["id"]) for row in rows}
    retriever = get_hybrid_retriever()
    # The full search would also surface the row through the recency signal, so
    # the signature arm is measured on its own.
    ranked = retriever._keyword_search(
        "ConnectionRefusedError: connection refused on db.internal:5432",
        USER,
        TENANT,
        10,
        HybridSearchOptions(tenant_id=TENANT, limit=10),
    )

    assert expected & set(ranked), (
        "a signature stored as the bare error class did not match a detailed failure of that class"
    )


def test_new_evidence_on_a_known_lesson_is_reported_as_a_write():
    """A lesson whose evidence changed is a write; an unchanged one is not."""
    _distill_failing_episode("lesson-edit-a")

    first = distill_experiences(user_id=USER, tenant_id=TENANT)
    assert first["procedural_memories_written"] == 1

    # Same episodes, same lesson: nothing to rewrite.
    repeat = distill_experiences(user_id=USER, tenant_id=TENANT)
    assert repeat["procedural_memories_written"] == 0

    # A second failure in the same signature group changes the occurrence count
    # in the lesson, so this run does rewrite the row.
    _distill_failing_episode("lesson-edit-b")

    updated = distill_experiences(user_id=USER, tenant_id=TENANT)
    assert updated["procedural_memories_written"] == 1


def test_rebuilding_an_episode_never_clears_sensitivity():
    session_id = _session_id()
    sensitive = [
        {"role": "user", "content": "patient diagnosis: prescription dosage adjustment needed"},
        {"role": "assistant", "content": "noted the medication and dosage for the patient"},
    ]
    episode = _episode_for(session_id, sensitive)
    assert episode["is_sensitive"] == 1

    # An incremental batch with benign messages must not clear the flag.
    rebuilt = _episode_for(session_id, [{"role": "user", "content": "continue the session"}])
    assert rebuilt["is_sensitive"] == 1
    stored = get_episode(session_id=session_id, user_id=USER, tenant_id=TENANT)
    assert stored is not None
    assert stored["is_sensitive"] is True


def test_distiller_pages_past_the_first_batch():
    for _ in range(EXPECTED_PAGED_EPISODES):
        session_id = _session_id()
        _episode_for(session_id, _failure_transcript())
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

    result = distill_experiences(user_id=USER, tenant_id=TENANT, limit=2)
    assert result["episodes_scanned"] == EXPECTED_PAGED_EPISODES


def test_recipe_steps_drop_text_the_secret_policy_only_flagged(monkeypatch):
    """`flag` mode returns the original text, so a flagged step must not survive."""
    events = [
        {"role": "user", "content": "rotate the deploy credentials"},
        {"role": "assistant", "content": "exported AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE into the job"},
        {"role": "assistant", "content": "ran the smoke checks against staging"},
    ]
    monkeypatch.setattr("foresight.experience._raw_events_for_session", lambda *_a, **_k: events)

    set_secret_defense_policy("flag", tenant_id=TENANT)
    try:
        steps = _procedure_steps(TENANT, USER, "sess-flagged-step")
    finally:
        reset_secret_defense_policies()

    assert steps == ["ran the smoke checks against staging"]


def test_distiller_invalidates_the_retriever_once_per_run(monkeypatch):
    calls: list[tuple[str, str]] = []

    class _Recorder:
        def invalidate_caches(self, user_id: str, tenant_id: str) -> None:
            calls.append((user_id, tenant_id))

    monkeypatch.setattr("foresight.experience.get_hybrid_retriever", _Recorder)

    _distill_failing_episode("invalidate-a")
    _distill_failing_episode("invalidate-b")
    session_id = _session_id()
    _episode_for(session_id, _success_transcript("invalidate-c"))
    record_outcome(
        USER,
        TENANT,
        EpisodeVerdict(outcome="success", score_source="ci_result", session_id=session_id),
    )

    result = distill_experiences(user_id=USER, tenant_id=TENANT)

    # Several groups are distilled per run; the cache is invalidated once at the
    # end instead of once per written memory.
    assert result["procedural_memories_written"] > 1
    assert calls == [(USER, TENANT)]

    # A run that only retires a stale group must still flush, once.
    calls.clear()
    for session in _fetch_all(
        "SELECT session_id FROM episodes WHERE tenant_id = ? AND user_id = ? AND outcome = 'failure'",
        (TENANT, USER),
    ):
        # An inconclusive re-score retires the group without forming a new
        # success group, so the flush below can only come from a retirement.
        record_outcome(
            USER,
            TENANT,
            EpisodeVerdict(outcome="unknown", score_source="ci_result", session_id=str(session["session_id"])),
        )

    retired_run = distill_experiences(user_id=USER, tenant_id=TENANT)

    assert retired_run["groups_retired"] >= 1
    assert retired_run["procedural_memories_written"] == 0
    assert calls == [(USER, TENANT)]


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


def _bind_caller(role: Role = Role.USER) -> object:
    return set_caller_identity(
        User(
            user_id="caller-user",
            username="caller",
            email="caller@test.local",
            role=role,
            api_key="caller-key",
        )
    )


def test_record_outcome_rejects_another_users_episode():
    token = _bind_caller()
    try:
        raw = record_episode_outcome(
            RecordEpisodeOutcomeParams(
                session_id="exp-does-not-exist",
                outcome="failure",
                score_source="ci_result",
                detail="ConnectionRefusedError: connection refused",
                user_id="somebody-else",
            )
        )
    finally:
        reset_caller_identity(token)
    payload = json.loads(raw)
    assert payload["ok"] is False
    # The denial happens before the episode lookup, so a missing session never
    # masks the authorization failure.
    assert "requires an admin API key" in payload["error"]["message"]


def test_record_outcome_without_a_bound_caller_keeps_the_user_override():
    raw = record_episode_outcome(
        RecordEpisodeOutcomeParams(
            session_id="exp-does-not-exist",
            outcome="failure",
            score_source="ci_result",
            detail="ConnectionRefusedError: connection refused",
            user_id="legacy-other-user",
        )
    )
    payload = json.loads(raw)
    # No principal is bound (CLI, single-user servers, in-process jobs), so the
    # explicit user override is honoured exactly as before the authorization fix.
    assert payload["ok"] is True
    assert payload["episode"]["user_id"] == "legacy-other-user"


def test_recorded_outcome_is_refused_when_auth_is_enforced_without_a_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforced auth with no bound principal must not fall back to the configured owner."""
    monkeypatch.setenv("FORESIGHT_REQUIRE_API_KEY", "1")
    raw = record_episode_outcome(
        RecordEpisodeOutcomeParams(
            session_id="exp-does-not-exist",
            outcome="failure",
            score_source="ci_result",
            detail="ConnectionRefusedError: connection refused",
        )
    )
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert "no caller identity" in payload["error"]["message"]


def test_a_failure_the_user_only_described_does_not_score_the_run_mixed():
    """The request quotes the old error; the run itself ended green."""
    session_id = _session_id()
    messages = [
        {
            "role": "user",
            "content": "the deploy failed with ConnectionRefusedError on db.internal:5432, fix it",
        },
        {"role": "assistant", "content": "raised the pool size and re-ran the deploy"},
        {"role": "tool", "content": "52 passed in 3.1s\nexit code 0"},
    ]
    episode = _episode_for(session_id, messages)

    scored = auto_score_episode(episode["id"], user_id=USER, tenant_id=TENANT, messages=messages)

    assert scored is not None
    assert scored["outcome"] == "success"


def test_flagged_transcripts_never_reach_the_llm_rubric(monkeypatch):
    """`flag` mode hands back the original text, so it may not reach a provider."""
    monkeypatch.setenv("FORESIGHT_EPISODE_SCORING", "llm")
    calls: list[tuple] = []

    def _record_call(*args, **_kwargs):
        calls.append(args)
        return 1.0, "should not run"

    monkeypatch.setattr("foresight.experience._llm_rubric", _record_call)
    messages = [
        {"role": "assistant", "content": "wrote AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE into the job env"},
        {"role": "tool", "content": "52 passed in 3.1s\nexit code 0"},
    ]

    set_secret_defense_policy("flag", tenant_id=TENANT)
    try:
        session_id = _session_id()
        episode = _episode_for(session_id, messages)
        scored = auto_score_episode(episode["id"], user_id=USER, tenant_id=TENANT, messages=messages)
    finally:
        reset_secret_defense_policies()

    assert calls == []
    assert scored is not None
    assert scored["score_source"] == "fallback"


def test_error_signature_is_derived_from_the_redacted_transcript(monkeypatch):
    """A credential in the failure text must not become the searchable signature."""
    secret = "AKIAIOSFODNN7EXAMPLE"
    seen: list[str] = []

    def _record(text):
        seen.append(text or "")
        return "clienterror: recorded"

    monkeypatch.setattr("foresight.experience.normalize_error_signature", _record)
    messages = [
        {
            "role": "tool",
            "content": (f"deploy failed: botocore.exceptions.ClientError: InvalidClientTokenId {secret}\nexit code 1"),
        },
    ]

    set_secret_defense_policy("flag", tenant_id=TENANT)
    try:
        session_id = _session_id()
        episode = _episode_for(session_id, messages)
        scored = auto_score_episode(episode["id"], user_id=USER, tenant_id=TENANT, messages=messages)
    finally:
        reset_secret_defense_policies()

    assert scored is not None
    assert seen, "the scorer must derive a signature"
    assert secret not in seen[0]
    assert "InvalidClientTokenId" in seen[0]


class _StubAuthManager:
    def __init__(self, user: User) -> None:
        self._user = user

    def get_user_by_id(self, user_id: str) -> User | None:
        return self._user if user_id == self._user.user_id else None

    def validate_user_tenant_access(self, user: User, tenant_id: str) -> bool:
        return user.user_id == self._user.user_id and tenant_id == TENANT


def test_a_readonly_caller_cannot_record_an_outcome():
    caller = User(
        user_id="readonly-verdict-user",
        username="readonly-verdict",
        email="readonly-verdict@test.local",
        role=Role.READONLY,
    )
    session_id = _session_id()
    _episode_for(session_id, _failure_transcript())

    token = set_caller_identity(caller)
    try:
        raw = record_episode_outcome(
            RecordEpisodeOutcomeParams(
                session_id=session_id,
                user_id=caller.user_id,
                outcome="failure",
                score_source="ci_result",
                detail="FAILED deploy - ConnectionRefusedError: connection refused",
            )
        )
    finally:
        reset_caller_identity(token)

    payload = json.loads(raw)
    assert payload["ok"] is False, payload
    assert "requires a user or admin API key" in payload["error"]["message"], payload


def test_an_authorized_oauth_call_records_a_verdict(monkeypatch):
    """A bearer token the OAuth provider authenticated must save a verdict.

    The provider verifies the token but binds no principal, so the middleware
    has to resolve the token's subject before the tool body's caller check runs.
    """
    caller = User(
        user_id="oauth-verdict-user",
        username="oauth-verdict",
        email="oauth-verdict@test.local",
        role=Role.USER,
    )
    monkeypatch.setenv("FORESIGHT_OAUTH_BASE_URL", "https://foresight.example.test")
    monkeypatch.setattr(auth_module, "_auth_manager", _StubAuthManager(caller))

    session_id = _session_id()
    _episode_for(session_id, _failure_transcript())

    access = AccessToken(
        token="oauth-access-token",
        client_id="client",
        scopes=["foresight.write"],
        subject=caller.user_id,
    )

    async def call_next(_context):
        return record_episode_outcome(
            RecordEpisodeOutcomeParams(
                session_id=session_id,
                user_id=caller.user_id,
                outcome="failure",
                score_source="ci_result",
                detail="FAILED deploy - ConnectionRefusedError: connection refused",
            )
        )

    context = SimpleNamespace(
        message=SimpleNamespace(
            name="record_episode_outcome",
            arguments={"tenant_id": TENANT},
            meta=SimpleNamespace(model_extra={}),
        )
    )

    token = auth_context_var.set(AuthenticatedUser(access))
    try:
        raw = asyncio.run(OAuthCallerMiddleware().on_call_tool(context, call_next))
    finally:
        auth_context_var.reset(token)

    payload = json.loads(raw)
    assert payload["ok"] is True, payload
    assert payload["episode"]["outcome"] == "failure"


def test_error_signature_comes_from_the_result_output_not_the_request():
    """A problem the request names is not this session's failure."""
    messages = [
        {"role": "user", "content": "fix the deploy: ConnectionRefusedError: connection refused on 10.0.0.8:5432"},
        {"role": "assistant", "content": "reading the socket teardown path"},
        {
            "role": "tool",
            "content": 'Traceback (most recent call last):\n  File "parser.py", line 12\nValueError: bad input token',
        },
    ]
    session_id = _session_id()
    episode = _episode_for(session_id, messages)

    scored = auto_score_episode(episode["id"], user_id=USER, tenant_id=TENANT, messages=messages)

    assert scored is not None
    assert "valueerror" in (scored["error_signature"] or "")
    assert "connectionrefused" not in (scored["error_signature"] or "")


def test_a_result_report_alone_does_not_justify_a_reusable_recipe(monkeypatch):
    session_id = _session_id()
    messages = [
        {"role": "user", "content": "run the billing release checklist for the result report case"},
        {"role": "assistant", "content": "52 passed in 3.1s"},
        {"role": "tool", "content": "exit code 0"},
    ]
    episode = _episode_for(session_id, messages)
    record_outcome(
        USER,
        TENANT,
        EpisodeVerdict(outcome="success", score_source="ci_result", session_id=session_id),
    )
    events = [
        {"role": "assistant", "content": "52 passed in 3.1s"},
        {"role": "tool", "content": "exit code 0"},
    ]
    monkeypatch.setattr("foresight.experience._raw_events_for_session", lambda *_a, **_k: events)

    result = distill_experiences(user_id=USER, tenant_id=TENANT)

    rows = _fetch_all(
        "SELECT observation_text FROM observations WHERE tenant_id = ? AND user_id = ? "
        "AND pattern_key LIKE ? AND supporting_episode_ids LIKE ?",
        (TENANT, USER, "success:%", f"%{episode['id']}%"),
    )
    assert rows, "the success observation is kept"
    assert "Reusable recipe" not in rows[0]["observation_text"]
    assert result["recipes_with_steps"] == 0


def test_distiller_flushes_recall_when_a_later_group_write_raises(monkeypatch):
    calls: list[tuple[str, str]] = []

    class _Recorder:
        def invalidate_caches(self, user_id: str, tenant_id: str) -> None:
            calls.append((user_id, tenant_id))

    monkeypatch.setattr("foresight.experience.get_hybrid_retriever", _Recorder)

    real_upsert = experience_module.upsert_episode_observation

    def _fail_on_a_success_observation(spec, *, user_id, tenant_id):
        if spec.pattern_key.startswith("success:"):
            raise RuntimeError("observation write failed")
        return real_upsert(spec, user_id=user_id, tenant_id=tenant_id)

    monkeypatch.setattr("foresight.experience.upsert_episode_observation", _fail_on_a_success_observation)

    _distill_failing_episode("flush-a")
    session_id = _session_id()
    _episode_for(session_id, _success_transcript("flush-b"))
    record_outcome(
        USER,
        TENANT,
        EpisodeVerdict(outcome="success", score_source="ci_result", session_id=session_id),
    )

    with pytest.raises(RuntimeError, match="observation write failed"):
        distill_experiences(user_id=USER, tenant_id=TENANT)

    # The failure group committed before the success write raised, so the recall
    # miss it left behind has to be evicted anyway, once.
    assert calls == [(USER, TENANT)]


def test_failure_group_keeps_each_episode_with_its_own_task():
    task_a = "deploy the billing service to staging before the window"
    task_b = "restart the analytics worker pool after the drain"
    failure_event = {
        "role": "tool",
        "content": "Traceback (most recent call last):\nConnectionRefusedError: connection refused on db.internal:5432",
    }
    ep_a = _episode_for(_session_id(), [{"role": "user", "content": task_a}, failure_event])
    ep_b = _episode_for(_session_id(), [{"role": "user", "content": task_b}, failure_event])
    for ep in (ep_a, ep_b):
        record_outcome(
            USER,
            TENANT,
            EpisodeVerdict(
                outcome="failure",
                score_source="ci_result",
                detail="ConnectionRefusedError: connection refused",
                session_id=ep["session_id"],
            ),
        )
    ep_ok = _episode_for(
        _session_id(),
        [
            {"role": "user", "content": task_b},
            {"role": "tool", "content": "restarted the worker pool and the queue drained cleanly"},
        ],
    )
    record_outcome(
        USER,
        TENANT,
        EpisodeVerdict(outcome="success", score_source="ci_result", session_id=ep_ok["session_id"]),
    )

    distill_experiences(user_id=USER, tenant_id=TENANT)

    rows = _fetch_all(
        "SELECT supporting_episode_ids FROM observations WHERE tenant_id = ? AND user_id = ? AND pattern_key LIKE ?",
        (TENANT, USER, "success:%"),
    )
    targets = [set(json.loads(row["supporting_episode_ids"] or "[]")) for row in rows]
    target = next((ids for ids in targets if ep_b["id"] in ids), None)
    assert target is not None, "task B's recipe must cite task B's own failure"
    assert ep_a["id"] not in target


def test_exact_signature_matches_rank_ahead_of_class_matches():
    """A class match may fill the window, but never by pushing the exact one out."""
    markers = ("rank-alpha", "rank-beta", "rank-gamma")
    for marker in markers:
        session_id = _session_id()
        _episode_for(session_id, _failure_transcript())
        record_outcome(
            USER,
            TENANT,
            EpisodeVerdict(
                outcome="failure",
                score_source="ci_result",
                detail=f"ValueError: bad input {marker}",
                session_id=session_id,
            ),
        )
    distill_experiences(user_id=USER, tenant_id=TENANT)

    rows = _fetch_all(
        "SELECT id FROM memories WHERE tenant_id = ? AND user_id = ? AND memory_type = 'procedural' "
        "AND error_signature LIKE ? ORDER BY id",
        (TENANT, USER, "valueerror: bad input rank-%"),
    )
    ids = [str(row["id"]) for row in rows]
    assert len(ids) == len(markers), "one distilled lesson per distinct failure detail"
    exact, class_a, class_b = ids[0], ids[1], ids[2]
    shared_content = "unrelated note about release choreography"
    query = "ValueError: bad input on 10.20.30.40:9999"
    exact_signature = normalize_error_signature(query)
    assert exact_signature
    assert exact_signature != query
    _execute(
        "UPDATE memories SET error_signature = ?, content = ?, importance = 0.05, created_at = ? WHERE id = ?",
        (exact_signature, shared_content, "2020-01-01T00:00:00Z", exact),
    )
    for mid in (class_a, class_b):
        _execute(
            "UPDATE memories SET error_signature = ?, content = ?, importance = 1.0, created_at = ? WHERE id = ?",
            ("valueerror: bad input on config.yml", shared_content, "2026-11-26T00:00:00Z", mid),
        )

    ranked = get_hybrid_retriever()._keyword_search(
        query,
        USER,
        TENANT,
        2,
        HybridSearchOptions(tenant_id=TENANT, limit=2),
    )

    assert exact in ranked, "the exact normalized-message match must survive the candidate limit"
