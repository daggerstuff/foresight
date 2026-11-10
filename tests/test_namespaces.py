"""Phase-4 namespace lanes: agent_id/project scoping and shared context blocks."""

from __future__ import annotations

import re

import pytest

from foresight.server import (
    USER_ID,
    inject_context,
    manage_context_blocks,
    manage_memories,
    search_memories,
)
from foresight.tenant_context import reset_tenant_context, set_current_account_id, set_current_user_id


@pytest.fixture(autouse=True)
def setup_test_ctx():
    set_current_user_id("_test_user_")
    set_current_account_id("_test_")
    yield
    reset_tenant_context()


def _store(content: str, **kwargs) -> str:
    result = manage_memories(action="store", content=content, category="fact", **kwargs)
    match = re.search(r"Stored memory (\S+)\.", result)
    assert match, result
    return match.group(1)


def test_agents_cannot_see_each_others_memories():
    marker_a = f"agent-a-secret-{USER_ID}"
    marker_b = f"agent-b-secret-{USER_ID}"
    _store(f"private note for agent A: {marker_a}", agent_id="agent-a")
    _store(f"private note for agent B: {marker_b}", agent_id="agent-b")

    res_a = search_memories(query="private note", agent_id="agent-a")
    res_b = search_memories(query="private note", agent_id="agent-b")

    assert marker_a in res_a
    assert marker_b not in res_a
    assert marker_b in res_b
    assert marker_a not in res_b


def test_default_lane_visible_to_all_agents():
    marker = f"shared-default-{USER_ID}"
    _store(f"shared note everyone can see: {marker}")

    for agent in ("agent-a", "agent-b"):
        res = search_memories(query="shared note", agent_id=agent)
        assert marker in res


def test_unscoped_search_sees_all_lanes():
    marker_a = f"lane-a-{USER_ID}"
    marker_b = f"lane-b-{USER_ID}"
    _store(f"namespaced memory alpha {marker_a}", agent_id="agent-a")
    _store(f"namespaced memory beta {marker_b}", agent_id="agent-b")

    res = search_memories(query="namespaced memory")
    assert marker_a in res
    assert marker_b in res


def test_project_lane_isolation():
    marker_p1 = f"proj-one-{USER_ID}"
    marker_p2 = f"proj-two-{USER_ID}"
    _store(f"project scoped memory {marker_p1}", project="proj-1")
    _store(f"project scoped memory {marker_p2}", project="proj-2")

    res_p1 = search_memories(query="project scoped memory", project="proj-1")
    res_p2 = search_memories(query="project scoped memory", project="proj-2")

    assert marker_p1 in res_p1
    assert marker_p2 not in res_p1
    assert marker_p2 in res_p2
    assert marker_p1 not in res_p2


def test_inject_context_respects_namespace():
    marker_a = f"inject-agent-a-{USER_ID}"
    _store(f"injection candidate zebra {marker_a}", agent_id="agent-a")

    out_a = inject_context(conversation_text="zebra injection candidate", agent_id="agent-a")
    out_b = inject_context(conversation_text="zebra injection candidate", agent_id="agent-b")

    assert marker_a in out_a
    assert marker_a not in out_b


def test_private_context_block_isolated_shared_block_visible():
    priv = f"agent-a-private-pref-{USER_ID}"
    shared = f"team-wide-pref-{USER_ID}"

    manage_context_blocks(
        action="update",
        label="user_preferences",
        content=f"PRIVATE {priv}",
        agent_id="agent-a",
        project="proj-x",
        visibility="private",
    )
    # Second block under a different label stays private to agent B's lane check
    manage_context_blocks(
        action="update",
        label="project_context",
        content=f"SHARED {shared}",
        agent_id="agent-a",
        project="proj-x",
        visibility="shared",
    )

    list_a = manage_context_blocks(action="list", agent_id="agent-a", project="proj-x")
    list_b = manage_context_blocks(action="list", agent_id="agent-b", project="proj-x")
    list_p2 = manage_context_blocks(action="list", agent_id="agent-a", project="proj-y")

    assert priv in list_a
    assert priv not in list_b
    assert shared in list_a
    assert shared in list_b
    # Shared block is project-scoped: invisible from another project lane.
    assert shared not in list_p2
