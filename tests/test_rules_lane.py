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
from foresight.observations import EpisodeObservationSpec, list_observations, upsert_episode_observation
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
    sensitive_obs = upsert_episode_observation(
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


def test_package_root_exports_every_public_name():
    missing = [name for name in foresight.__all__ if not hasattr(foresight, name)]
    assert missing == []
