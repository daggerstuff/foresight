"""Tests for ContextBlockAgent transcript processing.

Verifies that context blocks are correctly populated from both user and
assistant messages, that preference triggers are broad enough to catch
natural language, and that pending-item extraction is selective (only the
relevant sentence, not the entire message).
"""

import os
import tempfile

import pytest

from foresight.connection_pool import reset_pool
from foresight.subconscious import (
    PENDING_ITEMS,
    PROJECT_CONTEXT,
    USER_PREFERENCES,
    ContextBlockAgent,
)

SESSION_ID = "test-session-001"


@pytest.fixture
def agent(monkeypatch):
    """Fresh agent with an isolated SQLite DB so blocks start at defaults.

    Uses monkeypatch to route the ContextBlockAgent to a temp SQLite file
    instead of the shared Postgres backend managed by conftest.py.
    """
    fd, db_path = tempfile.mkstemp(suffix=".db")
    monkeypatch.setattr("foresight.subconscious.DB_PATH", db_path)
    reset_pool()
    a = ContextBlockAgent(user_id="test-user", tenant_id="test-tenant")
    yield a
    a.state.initialize_defaults()
    reset_pool()
    os.close(fd)
    os.unlink(db_path)


# ---------------------------------------------------------------------------
# Preference extraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "phrase",
    [
        "I always use pnpm for package management",
        "I prefer dark mode when coding",
        "I want to use uv for all python commands",
        "I'd like the tests to run in parallel",
        "I like using Tailwind for styling",
        "I usually start with the backend first",
        "Never use npm in this project",
        "Always use strict TypeScript",
        "Please use semantic commit messages",
        "Make sure to run lint before committing",
        "From now on, use vitest for tests",
        "Going forward, avoid using any",
        "I'd rather use FastAPI over Flask",
    ],
)
def test_broad_preference_triggers(agent, phrase):
    """Preferences are extracted from a wide range of natural phrases."""
    agent._process_user_message(phrase, SESSION_ID)
    block = agent.state.get_block(USER_PREFERENCES)
    assert not block.is_empty(), f"Preference not captured for: {phrase}"
    assert phrase[:30] in block.content


def test_non_preference_not_captured(agent):
    """Messages without preference signals should not populate preferences."""
    agent._process_user_message("The weather is nice today.", SESSION_ID)
    block = agent.state.get_block(USER_PREFERENCES)
    assert block.is_empty()


# ---------------------------------------------------------------------------
# Pending item extraction — selectivity
# ---------------------------------------------------------------------------

def test_pending_extracts_only_relevant_sentence(agent):
    """Only the sentence containing the trigger should be stored, not the
    entire message."""
    long_msg = (
        "The login page looks great. "
        "We should add OAuth support next. "
        "Also the styling needs some tweaks."
    )
    agent._process_user_message(long_msg, SESSION_ID)
    block = agent.state.get_block(PENDING_ITEMS)
    assert not block.is_empty()
    # The extracted snippet should contain "OAuth" but NOT "login page" or "styling".
    assert "OAuth" in block.content
    assert "login page" not in block.content.lower()
    assert "styling" not in block.content.lower()


def test_pending_does_not_match_bare_should(agent):
    """Bare 'should' in normal prose should not trigger pending extraction."""
    agent._process_user_message("This should work fine now.", SESSION_ID)
    block = agent.state.get_block(PENDING_ITEMS)
    assert block.is_empty(), f"False positive pending item: {block.content}"


def test_pending_compound_we_should(agent):
    """'we should' as a compound phrase should trigger pending extraction."""
    agent._process_user_message("We should refactor the auth module next.", SESSION_ID)
    block = agent.state.get_block(PENDING_ITEMS)
    assert not block.is_empty()
    assert "refactor" in block.content.lower()


def test_pending_todo_trigger(agent):
    """TODO keyword should trigger pending extraction."""
    agent._process_user_message("TODO: fix the failing tests in users.py", SESSION_ID)
    block = agent.state.get_block(PENDING_ITEMS)
    assert not block.is_empty()
    assert "fix" in block.content.lower()


def test_pending_follow_up_trigger(agent):
    """'follow up' phrase should trigger pending extraction."""
    agent._process_user_message("We need to follow up on the API migration.", SESSION_ID)
    block = agent.state.get_block(PENDING_ITEMS)
    assert not block.is_empty()
    assert "follow up" in block.content.lower()


# ---------------------------------------------------------------------------
# Assistant message processing
# ---------------------------------------------------------------------------

def test_assistant_message_extracts_project_context(agent):
    """Assistant messages with architectural decisions should populate
    project_context."""
    assistant_msg = (
        "I've refactored the API module in src/api/users.py to use a "
        "queue-based pipeline for better throughput."
    )
    agent._process_assistant_message(assistant_msg, SESSION_ID)
    block = agent.state.get_block(PROJECT_CONTEXT)
    assert not block.is_empty(), "Project context not extracted from assistant message"
    assert "refactored" in block.content.lower() or "queue" in block.content.lower()


def test_assistant_message_does_not_extract_preference(agent):
    """Preferences should never be extracted from assistant messages."""
    agent._process_assistant_message("I always use the latest Python version.", SESSION_ID)
    pref_block = agent.state.get_block(USER_PREFERENCES)
    assert pref_block.is_empty(), "Preference incorrectly extracted from assistant message"


def test_assistant_message_extracts_pending(agent):
    """Assistant messages mentioning follow-up tasks should capture pending items."""
    agent._process_assistant_message(
        "The migration is done. We still need to update the documentation in docs/api.md.",
        SESSION_ID,
    )
    block = agent.state.get_block(PENDING_ITEMS)
    assert not block.is_empty()
    assert "documentation" in block.content.lower() or "docs" in block.content.lower()


# ---------------------------------------------------------------------------
# process_transcript — full integration
# ---------------------------------------------------------------------------

async def test_process_transcript_handles_both_roles(agent):
    """process_transcript should process both user and assistant messages."""
    messages = [
        {"role": "user", "content": "I prefer using pnpm over npm for this project."},
        {"role": "assistant", "content": "Got it. I've updated the config in src/config.ts to use the new middleware layer."},
        {"role": "user", "content": "We should add tests for the new module next."},
    ]
    await agent.process_transcript(SESSION_ID, messages)

    pref_block = agent.state.get_block(USER_PREFERENCES)
    ctx_block = agent.state.get_block(PROJECT_CONTEXT)
    pending_block = agent.state.get_block(PENDING_ITEMS)

    assert not pref_block.is_empty(), "Preferences not captured from user message"
    assert not ctx_block.is_empty(), "Project context not captured from assistant message"
    assert not pending_block.is_empty(), "Pending items not captured from user message"


async def test_process_transcript_empty_messages(agent):
    """process_transcript should handle empty message lists gracefully."""
    await agent.process_transcript(SESSION_ID, [])
    # No crash, blocks remain at defaults.
    assert agent.state.get_block(USER_PREFERENCES).is_empty()
