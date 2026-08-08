"""Tests for ContextBlockAgent transcript processing.

Verifies that context blocks are correctly populated from both user and
assistant messages, that preference triggers are broad enough to catch
natural language, that pending-item extraction is selective (only the
relevant sentence, not the entire message), and that session patterns,
enhanced project context, and assistant self-correction preferences are
captured.  Also covers the capture-pipeline ↔ context-block bridge.
"""

import os
import tempfile

import pytest

from foresight.connection_pool import reset_pool
from foresight.subconscious import (
    PENDING_ITEMS,
    PROJECT_CONTEXT,
    SESSION_PATTERNS,
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


# ---------------------------------------------------------------------------
# Session patterns extraction
# ---------------------------------------------------------------------------

async def test_session_patterns_hot_files(agent):
    """Files mentioned 2+ times should appear in session_patterns."""
    messages = [
        {"role": "user", "content": "Can you check src/api/users.py for the bug?"},
        {"role": "assistant", "content": "I looked at src/api/users.py and found the issue."},
        {"role": "user", "content": "Great, fix src/api/users.py then."},
    ]
    await agent.process_transcript(SESSION_ID, messages)
    block = agent.state.get_block(SESSION_PATTERNS)
    assert not block.is_empty(), "Session patterns not extracted"
    assert "src/api/users.py" in block.content
    assert "Hot files" in block.content


async def test_session_patterns_common_tools(agent):
    """Tools mentioned 2+ times should appear in session_patterns."""
    messages = [
        {"role": "user", "content": "Run pytest to check the tests."},
        {"role": "assistant", "content": "I ran pytest and 3 tests failed."},
        {"role": "user", "content": "Run pytest again after the fix."},
    ]
    await agent.process_transcript(SESSION_ID, messages)
    block = agent.state.get_block(SESSION_PATTERNS)
    assert not block.is_empty()
    assert "pytest" in block.content.lower()
    assert "Common tools" in block.content


async def test_session_patterns_recurring_errors(agent):
    """Errors mentioned 2+ times should appear in session_patterns."""
    messages = [
        {"role": "user", "content": "I'm getting a timeout error when calling the API."},
        {"role": "assistant", "content": "The timeout error is likely from the connection pool."},
        {"role": "user", "content": "Still seeing the timeout error after restarting."},
    ]
    await agent.process_transcript(SESSION_ID, messages)
    block = agent.state.get_block(SESSION_PATTERNS)
    assert not block.is_empty()
    assert "timeout" in block.content.lower()
    assert "Recurring errors" in block.content


async def test_session_patterns_no_repeats(agent):
    """When nothing is mentioned twice, session_patterns stays empty."""
    messages = [
        {"role": "user", "content": "Check src/config.ts for the setting."},
        {"role": "assistant", "content": "I ran eslint and it passed."},
    ]
    await agent.process_transcript(SESSION_ID, messages)
    block = agent.state.get_block(SESSION_PATTERNS)
    assert block.is_empty(), f"Unexpected patterns: {block.content}"


async def test_session_patterns_reset_per_transcript(agent):
    """Pattern counters should reset between transcripts."""
    messages = [
        {"role": "user", "content": "Check src/api/users.py"},
        {"role": "assistant", "content": "I checked src/api/users.py"},
    ]
    await agent.process_transcript(SESSION_ID, messages)
    assert not agent.state.get_block(SESSION_PATTERNS).is_empty()

    # Second transcript mentions the same file only once — should NOT produce a pattern.
    messages2 = [
        {"role": "user", "content": "Now look at src/config.ts for the port setting."},
    ]
    await agent.process_transcript(SESSION_ID + "-b", messages2)
    block = agent.state.get_block(SESSION_PATTERNS)
    # The second transcript should not have added a hot-file pattern for src/config.ts.
    assert "src/config.ts" not in block.content or "Hot files" not in block.content.split(SESSION_ID + "-b")[-1]


# ---------------------------------------------------------------------------
# Enhanced project context detection
# ---------------------------------------------------------------------------

def test_project_context_named_component(agent):
    """'the X module/service/component' pattern should trigger project context."""
    agent._process_assistant_message(
        "I updated the middleware to handle request throttling properly.",
        SESSION_ID,
    )
    block = agent.state.get_block(PROJECT_CONTEXT)
    assert not block.is_empty(), "Named component not detected as project context"


def test_project_context_bare_relative_path(agent):
    """Bare relative paths like ./src/config.ts should trigger project context."""
    agent._process_user_message(
        "Take a look at ./src/config.ts for the database settings.",
        SESSION_ID,
    )
    block = agent.state.get_block(PROJECT_CONTEXT)
    assert not block.is_empty(), "Relative path not detected as project context"


def test_project_context_stack_noun_with_verb(agent):
    """Stack noun + strong verb should trigger project context."""
    agent._process_assistant_message(
        "I refactored the database schema to use UUIDs instead of serial IDs.",
        SESSION_ID,
    )
    block = agent.state.get_block(PROJECT_CONTEXT)
    assert not block.is_empty(), "Stack noun + verb not detected"


def test_project_context_non_technical_rejected(agent):
    """Non-technical decisions should not pollute project context."""
    agent._process_user_message(
        "I decided to move to another city for a change of scenery.",
        SESSION_ID,
    )
    block = agent.state.get_block(PROJECT_CONTEXT)
    assert block.is_empty(), f"Non-technical content leaked into project context: {block.content}"


def test_project_context_bare_service_excluded(agent):
    """Bare 'service' in non-technical context should not trigger project context."""
    agent._process_user_message(
        "The customer service was excellent at the restaurant.",
        SESSION_ID,
    )
    block = agent.state.get_block(PROJECT_CONTEXT)
    assert block.is_empty(), f"'service' false positive: {block.content}"


# ---------------------------------------------------------------------------
# Assistant preference extraction (self-corrections)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "phrase",
    [
        "Actually, I'll use pnpm instead of npm for this project.",
        "Let me use uv for managing the virtual environment.",
        "I'll go with FastAPI since it's more modern.",
        "I'm going to use Tailwind for the styling.",
        "Better to use strict TypeScript here.",
        "I'd rather use vitest over jest.",
        "I prefer to use semantic commit messages.",
    ],
)
def test_assistant_preference_self_correction(agent, phrase):
    """Assistant self-correction phrases should extract preferences."""
    agent._process_assistant_message(phrase, SESSION_ID)
    block = agent.state.get_block(USER_PREFERENCES)
    assert not block.is_empty(), f"Preference not captured from assistant self-correction: {phrase}"
    assert "[Assistant preference]" in block.content


def test_assistant_preference_no_false_positive(agent):
    """Assistant messages without self-correction should not extract preferences."""
    agent._process_assistant_message(
        "The function returns a boolean value indicating success.",
        SESSION_ID,
    )
    block = agent.state.get_block(USER_PREFERENCES)
    assert block.is_empty(), f"False positive preference: {block.content}"


# ---------------------------------------------------------------------------
# Capture-pipeline ↔ context-block bridge
# ---------------------------------------------------------------------------

def test_capture_stats_stored_items():
    """CaptureStats should track stored_items as (category, content) tuples."""
    from foresight.capture import CaptureStats

    stats = CaptureStats()
    stats.stored_items.append(("preference", "I prefer pnpm over npm"))
    stats.stored_items.append(("pending_item", "TODO: fix tests"))
    d = stats.to_dict()
    assert ("preference", "I prefer pnpm over npm") in d["stored_items"]
    assert ("pending_item", "TODO: fix tests") in d["stored_items"]


def test_bridge_capture_memories_to_blocks(agent):
    """_bridge_capture_memories_to_blocks should sync memories to context blocks."""
    from foresight.server import _bridge_capture_memories_to_blocks

    stored_items = [
        ("preference", "I prefer pnpm over npm"),
        ("pending_item", "TODO: fix the failing tests"),
        ("pattern", "Used vitest for all test runs"),
        ("decision", "Migrated from Flask to FastAPI"),
    ]
    count = _bridge_capture_memories_to_blocks(agent, stored_items)
    assert count == 4

    pref = agent.state.get_block(USER_PREFERENCES)
    assert "I prefer pnpm over npm" in pref.content

    pending = agent.state.get_block(PENDING_ITEMS)
    assert "fix the failing tests" in pending.content

    patterns = agent.state.get_block(SESSION_PATTERNS)
    assert "Used vitest for all test runs" in patterns.content

    ctx = agent.state.get_block(PROJECT_CONTEXT)
    assert "Migrated from Flask to FastAPI" in ctx.content


def test_bridge_capture_dedup(agent):
    """_bridge_capture_memories_to_blocks should not duplicate existing content."""
    from foresight.server import _bridge_capture_memories_to_blocks

    items = [("preference", "I prefer pnpm over npm")]
    _bridge_capture_memories_to_blocks(agent, items)
    # Same item again — should be skipped.
    count = _bridge_capture_memories_to_blocks(agent, items)
    assert count == 0


def test_bridge_capture_ignores_unknown_category(agent):
    """Unknown categories (e.g. tool_recipe) should be silently skipped."""
    from foresight.server import _bridge_capture_memories_to_blocks

    count = _bridge_capture_memories_to_blocks(
        agent, [("tool_recipe", "npm install --save-dev vitest")]
    )
    assert count == 0
