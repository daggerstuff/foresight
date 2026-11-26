"""Tests for FOR-32: rules as a never-truncated lane."""

from __future__ import annotations

import json

import pytest

import foresight
from foresight.block_registry import (
    RULES_BLOCK_LABEL,
    BlockRegistry,
    RuleEntry,
    RuleStrength,
    initialize_default_blocks,
)
from foresight.context_blocks import (
    RuleGraduateOptions,
    add_rule,
    compile_experience_to_rule,
    get_context_block_agent,
    graduate_experience_to_rule,
    list_rules,
    retrieve_matching_rules,
)
from foresight.experience import EpisodeVerdict, build_episode, distill_experiences, record_outcome
from foresight.injection_budget import (
    InjectionBudget,
    Lane,
    LaneItem,
    allocate_lane,
    format_budgeted_payload,
    format_static_and_safety,
)
from foresight.observations import (
    EpisodeObservationSpec,
    _upsert_observation,
    list_observations,
    upsert_episode_observation,
)
from foresight.reflect import UserScope
from foresight.server import (
    InjectContextParams,
    ManageInferredParams,
    inject_context,
    manage_inferred,
)
from foresight.skill_crystallization import crystallize_skills
from foresight.tenant_context import reset_tenant_context, set_current_account_id, set_current_user_id

TENANT = "_test_rules_"
USER_ID = "_test_rules_user_"
EXPECTED_RULE_COUNT = 3
EXPECTED_TIGHT_RULE_COUNT = 2


@pytest.fixture(autouse=True)
def _tenant_ctx():
    set_current_user_id(USER_ID)
    set_current_account_id(TENANT)
    yield
    reset_tenant_context()


def test_rules_block_schema_registered_and_validated():
    previous = BlockRegistry._instance
    BlockRegistry._instance = None
    try:
        registry = initialize_default_blocks()
        schema = registry.get_schema(RULES_BLOCK_LABEL)
        assert schema is not None
        assert schema.metadata["fields"] == ["statement", "strength", "applies_when", "exception", "source"]
        assert schema.metadata["strengths"] == ["must", "must_not", "should"]

        agent = get_context_block_agent(USER_ID, TENANT)
        with pytest.raises(ValueError, match="Invalid content for block 'rules'"):
            agent.update_block(RULES_BLOCK_LABEL, json.dumps([{"statement": "Bad", "strength": "forbidden"}]))

        with pytest.raises(ValueError, match="Invalid content for block 'rules'"):
            agent.update_block(RULES_BLOCK_LABEL, json.dumps([{"statement": "", "strength": "must"}]))

        # Only the exact placeholder counts as empty; a lookalike prefix is malformed.
        with pytest.raises(ValueError, match="Invalid content for block 'rules'"):
            agent.update_block(RULES_BLOCK_LABEL, "(No such rules) not json")
    finally:
        BlockRegistry._instance = previous


def test_rules_retrieved_by_condition_and_exception():
    add_rule(
        RuleEntry(
            statement="Always run pytest before merging PRs",
            strength=RuleStrength.MUST,
            applies_when="merging pull requests",
            exception="docs only changes",
            source="team-policy",
        ),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    add_rule(
        RuleEntry(
            statement="Never force-push to main",
            strength=RuleStrength.MUST_NOT,
            applies_when="git push",
            exception="",
            source="git-guardrail",
        ),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    add_rule(
        RuleEntry(
            statement="Prefer uv over pip for Python dependencies",
            strength=RuleStrength.SHOULD,
            applies_when="always",
            source="repo-convention",
        ),
        user_id=USER_ID,
        tenant_id=TENANT,
    )

    assert len(list_rules(user_id=USER_ID, tenant_id=TENANT)) == EXPECTED_RULE_COUNT

    pr_matches = retrieve_matching_rules("I am merging pull requests now", user_id=USER_ID, tenant_id=TENANT)
    pr_statements = {r.statement for r in pr_matches}
    assert "Always run pytest before merging PRs" in pr_statements
    assert "Prefer uv over pip for Python dependencies" in pr_statements
    assert "Never force-push to main" not in pr_statements

    # Exception suppresses the PR rule.
    docs_matches = retrieve_matching_rules(
        "I am merging pull requests with docs only changes",
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    docs_statements = {r.statement for r in docs_matches}
    assert "Always run pytest before merging PRs" not in docs_statements
    assert "Prefer uv over pip for Python dependencies" in docs_statements


def test_rule_matching_respects_word_boundaries():
    rule = RuleEntry(statement="Run the CI suite before pushing", strength=RuleStrength.MUST, applies_when="ci")
    assert rule.matches("we made a decision about the release") is False
    assert rule.matches("run ci now") is True

    guarded = RuleEntry(
        statement="Squash before merge",
        strength=RuleStrength.SHOULD,
        applies_when="merge",
        exception="docs",
    )
    assert guarded.matches("the merger is complete") is False
    assert guarded.matches("merge the branch") is True
    assert guarded.matches("merge the docs") is False


def test_rule_matching_keeps_single_character_qualifiers():
    conditioned = RuleEntry(
        statement="Pin the interpreter to the 3.x line",
        strength=RuleStrength.MUST,
        applies_when="python 3",
    )
    assert conditioned.matches("deploy python 3 app") is True
    assert conditioned.matches("deploy python 2 app") is False

    guarded = RuleEntry(
        statement="Run the legacy migration",
        strength=RuleStrength.MUST,
        applies_when="deploy",
        exception="python 3",
    )
    assert guarded.matches("deploy python 3 app") is False
    assert guarded.matches("deploy python 2 app") is True


def test_experience_strength_is_inferred_per_clause():
    mixed = {
        "id": "obs_mixed_strength",
        "observation_text": "Never skip the smoke tests; consider running the full suite too.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    compiled = compile_experience_to_rule(mixed, requested_strength=RuleStrength.MUST_NOT)
    assert compiled.strength == RuleStrength.MUST_NOT

    recommendation_only = {
        "id": "obs_recommendation_only",
        "observation_text": "Consider running the full suite too.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(recommendation_only).strength == RuleStrength.SHOULD


def test_strength_follows_the_nearest_recommendation_before_a_never():
    """Two recommendations in one sentence leave the second one a recommendation."""
    paired = {
        "id": "obs_paired_recommendations",
        "observation_text": "You should check coverage and should never skip smoke tests.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(paired).strength == RuleStrength.SHOULD

    # A conjunction opens a new clause, so the prohibition the recommendation
    # and check it follows stands on its own.
    separate = {
        "id": "obs_separate_clause",
        "observation_text": "You should check coverage, and never skip smoke tests.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(separate).strength == RuleStrength.MUST_NOT


def test_rules_lane_never_truncated_under_tightest_budget():
    rule_items = [
        LaneItem(
            id="rule:1",
            content="[MUST] Never run destructive migrations without a backup. "
            "Second sentence that would be cut if summarized.",
            score=1.0,
            lane=Lane.RULES,
        ),
        LaneItem(
            id="rule:2",
            content="[MUST_NOT] Never commit plaintext secrets to any branch. "
            "Second sentence must also remain completely intact.",
            score=0.95,
            lane=Lane.RULES,
        ),
    ]
    memory_items = [
        LaneItem(id=f"m{i}", content=f"Long memory {i}. " + ("detail " * 30), score=0.5, lane=Lane.MEMORIES)
        for i in range(5)
    ]

    # Tightest possible budget (max_chars=0): memories get squeezed, rules stay 100% FULL.
    tight_budget = InjectionBudget(max_chars=0)
    alloc = allocate_lane(rule_items, Lane.RULES, tight_budget)
    assert len(alloc.full_items) == EXPECTED_TIGHT_RULE_COUNT
    # -1 is the unbounded sentinel: rules ignore the global cap entirely.
    assert alloc.budget_chars == -1
    assert alloc.summary_items == []
    assert alloc.stub_items == []
    assert alloc.omitted_count == 0

    payload = format_budgeted_payload(
        {Lane.RULES: rule_items, Lane.MEMORIES: memory_items},
        InjectionBudget(max_chars=20),
    )
    assert "[rules]" in payload.formatted
    assert "Second sentence that would be cut if summarized." in payload.formatted
    assert "Second sentence must also remain completely intact." in payload.formatted
    assert payload.allocations[Lane.RULES].omitted_count == 0
    assert payload.allocations[Lane.RULES].summary_items == []
    assert payload.allocations[Lane.RULES].stub_items == []

    static_safety = format_static_and_safety({Lane.RULES: rule_items, Lane.MEMORIES: memory_items})
    assert "[rules]" in static_safety
    assert "Second sentence must also remain completely intact." in static_safety
    assert "m0" not in static_safety


def test_compile_experience_never_upgrades_should_to_must():
    should_obs = {
        "id": "obs_should_1",
        "observation_text": "You should run ruff check before pushing commits.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    compiled_default = compile_experience_to_rule(should_obs)
    assert compiled_default.strength == RuleStrength.SHOULD

    # Even when caller requests 'must', compilation preserves 'should'.
    compiled_requested_must = compile_experience_to_rule(should_obs, requested_strength=RuleStrength.MUST)
    assert compiled_requested_must.strength == RuleStrength.SHOULD

    # A neutral procedural recipe defaults to 'should' and also never upgrades to 'must'.
    recipe_obs = {
        "id": "obs_recipe_1",
        "observation_text": '[procedural] Reusable recipe - "deploy staging": succeeded 3 time(s).',
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(recipe_obs, requested_strength="must").strength == RuleStrength.SHOULD

    # "should never" is a weak recommendation, not a prohibition.
    should_never = {
        "id": "obs_should_never",
        "observation_text": "You should never force-push to main.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(should_never).strength == RuleStrength.SHOULD

    # Explicit 'must' can stay 'must' or downgrade to 'should'.
    must_obs = {
        "id": "obs_must_1",
        "observation_text": "Deployments must verify schema parity before rolling out.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(must_obs).strength == RuleStrength.MUST
    assert compile_experience_to_rule(must_obs, requested_strength="should").strength == RuleStrength.SHOULD


def test_graduate_reviewed_experience_and_inject_under_tight_budget():
    for idx in range(3):
        sid = f"rule-ep-{idx}"
        build_episode(
            sid,
            user_id=USER_ID,
            tenant_id=TENANT,
            messages=[
                {"role": "user", "content": "deploying billing service safely"},
                {"role": "assistant", "content": "running smoke checks"},
                {"role": "tool", "content": "10 passed in 1.2s\nexit code 0"},
            ],
        )
        record_outcome(
            USER_ID,
            TENANT,
            EpisodeVerdict(outcome="success", score_source="ci_result", session_id=sid),
        )

    distill_experiences(user_id=USER_ID, tenant_id=TENANT)
    observations = list_observations(UserScope(user_id=USER_ID, tenant_id=TENANT), limit=20)
    obs = next(o for o in observations if o["memory_type"] == "procedural")

    # Unreviewed experience cannot graduate directly.
    with pytest.raises(ValueError, match="must be reviewed"):
        graduate_experience_to_rule(obs["id"], user_id=USER_ID, tenant_id=TENANT)

    # Sensitive procedural observations cannot graduate even with a review record.
    sensitive_obs, _ = upsert_episode_observation(
        EpisodeObservationSpec(
            pattern_key="success:sensitive-check",
            observation_text="Patient diagnosis prescription dosage must be verified.",
            category="procedural",
            confidence=0.9,
            episode_ids=[],
        ),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    with pytest.raises(ValueError, match="sensitive"):
        graduate_experience_to_rule(
            sensitive_obs["id"],
            user_id=USER_ID,
            tenant_id=TENANT,
            options=RuleGraduateOptions(),
        )

    # Approving the crystallized skill marks the experience reviewed.
    cryst = crystallize_skills(UserScope(user_id=USER_ID, tenant_id=TENANT))
    assert cryst["staged_count"] == 1
    manage_inferred(ManageInferredParams(action="approve", memory_id=cryst["skills"][0]["memory_id"], user_id=USER_ID))

    graduated = graduate_experience_to_rule(
        obs["id"],
        user_id=USER_ID,
        tenant_id=TENANT,
        options=RuleGraduateOptions(
            requested_strength=RuleStrength.MUST,  # Attempted upgrade must stay 'should'.
            applies_when="deploying billing service",
            exception="dry-run mode",
        ),
    )
    assert graduated.strength == RuleStrength.SHOULD
    assert graduated.source == f"observation:{obs['id']}"

    # inject_context under the tightest budget preserves the full rule when condition matches.
    raw = inject_context(
        InjectContextParams(
            conversation_text="We are deploying billing service right now",
            user_id=USER_ID,
            max_chars=10,
            include_details=True,
        )
    )
    payload = json.loads(raw)
    assert len(payload["rules"]) == 1
    assert payload["rules"][0]["strength"] == "should"
    assert "[rules]" in payload["formatted"]
    assert graduated.statement in payload["formatted"]
    assert "[rules]" in payload["static_and_safety"]

    # Exception prevents injection when dry-run mode is active.
    raw_exc = inject_context(
        InjectContextParams(
            conversation_text="We are deploying billing service in dry-run mode",
            user_id=USER_ID,
            max_chars=10,
            include_details=True,
        )
    )
    payload_exc = json.loads(raw_exc)
    assert payload_exc["rules"] == []


def test_graduation_requires_the_approved_skill_to_cover_the_current_observation_version():
    """Refining an observation must invalidate the review its skill relied on."""
    for idx in range(3):
        sid = f"rule-version-ep-{idx}"
        build_episode(
            sid,
            user_id=USER_ID,
            tenant_id=TENANT,
            messages=[
                {"role": "user", "content": "rotating deploy keys nightly"},
                {"role": "assistant", "content": "running the key rotation script"},
                {"role": "tool", "content": "10 passed in 1.2s\nexit code 0"},
            ],
        )
        record_outcome(
            USER_ID,
            TENANT,
            EpisodeVerdict(outcome="success", score_source="ci_result", session_id=sid),
        )

    distill_experiences(user_id=USER_ID, tenant_id=TENANT)
    observations = list_observations(UserScope(user_id=USER_ID, tenant_id=TENANT), limit=20)
    obs = next(o for o in observations if str(o["pattern_key"]).startswith("success:rotating-deploy-keys"))
    assert obs["version"] == 1

    cryst = crystallize_skills(UserScope(user_id=USER_ID, tenant_id=TENANT))
    manage_inferred(ManageInferredParams(action="approve", memory_id=cryst["skills"][0]["memory_id"], user_id=USER_ID))

    # The approval covers version 1's wording only. Refining the observation
    # produces text no reviewer has seen, so the old approval must not carry
    # over to it.
    refined, _ = upsert_episode_observation(
        EpisodeObservationSpec(
            pattern_key=obs["pattern_key"],
            observation_text=(
                '[procedural] Task - "rotating deploy keys nightly": succeeded 4 time(s), '
                "now rotated hourly by the scheduled job."
            ),
            category="procedural",
            confidence=0.9,
            episode_ids=[],
        ),
        user_id=USER_ID,
        tenant_id=TENANT,
    )
    assert refined["version"] == obs["version"] + 1

    with pytest.raises(ValueError, match="re-review is required"):
        graduate_experience_to_rule(obs["id"], user_id=USER_ID, tenant_id=TENANT)

    # Re-staging reopens the review, so approving the new text restores the path.
    restaged = crystallize_skills(UserScope(user_id=USER_ID, tenant_id=TENANT))
    assert restaged["updated_count"] == 1
    manage_inferred(
        ManageInferredParams(action="approve", memory_id=restaged["skills"][0]["memory_id"], user_id=USER_ID)
    )
    graduated = graduate_experience_to_rule(obs["id"], user_id=USER_ID, tenant_id=TENANT)
    assert graduated.source == f"observation:{obs['id']}"


def test_package_root_exports_every_public_name():
    missing = [name for name in foresight.__all__ if not hasattr(foresight, name)]
    assert missing == []


def test_rule_entry_from_dict_rejects_a_non_string_qualifier():
    # Stringifying False produced "", which loads a conditional rule as an
    # unconditional one: the guard has to reject it instead.
    with pytest.raises(ValueError, match=r"applies_when must be a string"):
        RuleEntry.from_dict({"statement": "Deploy only from CI", "strength": "must", "applies_when": False})

    with pytest.raises(ValueError, match=r"exception must be a string"):
        RuleEntry.from_dict({"statement": "Deploy only from CI", "strength": "must", "exception": ["x"]})

    rule = RuleEntry.from_dict({"statement": "Deploy only from CI", "strength": "must", "applies_when": None})
    assert rule.applies_when == ""
    assert rule.source == "manual"


def test_single_character_clause_token_must_be_present():
    rule = RuleEntry(statement="Pin the interpreter", applies_when="python 3")

    assert rule.matches("python 3.11 toolchain") is True
    assert rule.matches("python 2.7 toolchain") is False


def test_qualifier_matching_keeps_punctuation_significant_terms_apart():
    """Dropping punctuation folded "C++" and "C#" into a bare "c" token."""
    conditioned = RuleEntry(statement="Free the buffer before returning", applies_when="C++")
    assert conditioned.matches("build a C++ service") is True
    assert conditioned.matches("build a C# service") is False

    guarded = RuleEntry(statement="Keep the C++ build green", applies_when="deploy", exception="C++")
    assert guarded.matches("deploy the C# service") is True
    assert guarded.matches("deploy the C++ service") is False


def test_graduation_stays_inside_the_observations_lane() -> None:
    laned, _ = _upsert_observation(
        {
            "pattern_key": "procedural:laned-graduation",
            "observation_text": "Re-run the migration twice before graduating the rule.",
            "category": "procedural",
            "confidence": 0.9,
            "supporting_memory_ids": [],
            "supporting_episode_ids": ["ep_laned"],
            "memory_type": "procedural",
        },
        user_id=USER_ID,
        tenant_id=TENANT,
        agent_id="agent-a",
        project="proj-a",
    )

    # A caller with no lane reads the shared lane only, so another lane's
    # private observation is invisible to it.
    with pytest.raises(LookupError, match="not found"):
        graduate_experience_to_rule(laned["id"], user_id=USER_ID, tenant_id=TENANT)

    # Naming another lane does not reach it either: the lookup predicate is
    # that lane plus the shared lane.
    with pytest.raises(LookupError, match="not found"):
        graduate_experience_to_rule(
            laned["id"],
            user_id=USER_ID,
            tenant_id=TENANT,
            options=RuleGraduateOptions(agent_id="agent-b", project="proj-b"),
        )

    # The owning lane does reach it, and stops at the review gate.
    with pytest.raises(ValueError, match="must be reviewed"):
        graduate_experience_to_rule(
            laned["id"],
            user_id=USER_ID,
            tenant_id=TENANT,
            options=RuleGraduateOptions(agent_id="agent-a", project="proj-a"),
        )


def test_bare_qualifier_stays_off_punctuated_language_names():
    conditioned = RuleEntry(statement="Keep the C toolchain small", applies_when="c")
    assert conditioned.matches("build a c++ service") is False
    assert conditioned.matches("build a c# service") is False
    assert conditioned.matches("write the c helper") is True

    guarded = RuleEntry(statement="Keep the C build green", applies_when="deploy", exception="c")
    assert guarded.matches("deploy the c# tooling") is True
    assert guarded.matches("deploy the c tooling") is False


def test_strength_keeps_a_hostname_and_a_version_inside_their_clause():
    """A dot inside "api.example.com" or "3.11" is not a sentence break."""
    hostname = {
        "id": "obs_hostname",
        "observation_text": "You should configure api.example.com to never accept plaintext connections.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(hostname).strength == RuleStrength.SHOULD

    version = {
        "id": "obs_version",
        "observation_text": "Upgrade to python 3.11 and you should never pin the patch version.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(version).strength == RuleStrength.SHOULD


def test_comma_joined_must_before_a_should_stays_must():
    """Commas do not split statements, and the earlier "must" governs the clause."""
    combined = {
        "id": "obs_comma_must",
        "observation_text": "You must run the smoke tests, and should run the full suite when time allows.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(combined).strength == RuleStrength.MUST


def test_prohibition_after_a_recommendation_is_not_downgraded():
    mixed = {
        "id": "obs_should_then_must_not",
        "observation_text": "You must run the suite, should check coverage, and must not skip smoke tests.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(mixed).strength == RuleStrength.MUST_NOT

    governed = {
        "id": "obs_should_governs_never",
        "observation_text": "You should never skip the smoke tests.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(governed).strength == RuleStrength.SHOULD

    recommendation_only = {
        "id": "obs_should_only_after_break",
        "observation_text": "Run the suite, then should check coverage.",
        "category": "procedural",
        "memory_type": "procedural",
    }
    assert compile_experience_to_rule(recommendation_only).strength == RuleStrength.SHOULD
