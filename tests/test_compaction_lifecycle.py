"""Tests for compaction_lifecycle — combined capture + recovery for context compaction."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from foresight.server import compaction_lifecycle


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    """Setup test context with tenant context."""
    from foresight.tenant_context import set_current_account_id, set_current_user_id

    set_current_user_id("_compaction_test_user_")
    set_current_account_id("_compaction_test_")

    yield

    from foresight.tenant_context import reset_tenant_context

    reset_tenant_context()


def _mock_capture(session_id, messages, **kwargs):
    """Mock for process_session_transcript to avoid hitting LLM/Postgres during tests."""
    return f"Processed transcript for session {session_id} (0 new memories)"

    from foresight.tenant_context import reset_tenant_context

    reset_tenant_context()


def _insert_memory(conn, memory_id: str, content: str, **overrides):
    """Insert a memory row with sensible defaults."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO memories
        (id, content, content_hash, tenant_id, user_id, scope, retention, category,
         bank_id, created_at, updated_at, tags, emotional_context, metrics,
         is_ghost, synthesized_from, version, importance, activation_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO NOTHING""",
        (
            memory_id,
            content,
            overrides.get("content_hash"),
            overrides.get("tenant_id", "_compaction_test_"),
            overrides.get("user_id", "_compaction_test_user_"),
            overrides.get("scope", "session"),
            overrides.get("retention", "short_term"),
            overrides.get("category", "fact"),
            overrides.get("bank_id", "default"),
            overrides.get("created_at", now),
            overrides.get("updated_at", now),
            overrides.get("tags", "[]"),
            overrides.get("emotional_context", "{}"),
            overrides.get("metrics", "{}"),
            overrides.get("is_ghost", 0),
            overrides.get("synthesized_from", "[]"),
            overrides.get("version", 1),
            overrides.get("importance", 0.5),
            overrides.get("activation_count", 1),
        ),
    )


def _get_conn():
    """Get a direct connection to the test DB (for setup/assert)."""
    from foresight.server import get_db_connection

    return get_db_connection()


@patch("foresight.server.process_session_transcript", side_effect=_mock_capture)
def test_compaction_lifecycle_captures_and_recovers(_mock):
    """compaction_lifecycle processes transcript and returns recovery payload."""
    messages = [
        {"role": "user", "content": "Let's use Postgres for the new auth service."},
        {"role": "assistant", "content": "Great choice. I'll set up the schema with pg_trgm for fuzzy matching."},
        {"role": "user", "content": "Make sure we enable SSL on all connections."},
    ]

    result = compaction_lifecycle(
        session_id="compaction-test-1",
        messages=messages,
    )

    # Should contain both the capture confirmation and recovery payload
    assert "Processed transcript" in result
    assert "Recovery Context" in result
    assert "compaction-test-1" in result


@patch("foresight.server.process_session_transcript", side_effect=_mock_capture)
def test_compaction_lifecycle_empty_messages(_mock):
    """compaction_lifecycle handles empty message list gracefully."""
    result = compaction_lifecycle(
        session_id="compaction-empty",
        messages=[],
    )

    # Should still return a result with recovery payload (possibly empty)
    assert "Recovery Context" in result or "0 memories" in result


@patch("foresight.server.process_session_transcript", side_effect=_mock_capture)
def test_compaction_lifecycle_respects_exclude_ids(_mock):
    """exclude_memory_ids filters memories from the recovery payload."""
    conn = _get_conn()
    _insert_memory(
        conn,
        "compact-exclude-1",
        "This memory should be excluded after compaction.",
        scope="session",
        importance=0.9,
    )
    _insert_memory(
        conn,
        "compact-keep-1",
        "This memory should survive into the recovery payload.",
        scope="session",
        importance=0.8,
    )
    conn.close()

    result = compaction_lifecycle(
        session_id="compaction-exclude-test",
        messages=[{"role": "user", "content": "Just a test message."}],
        exclude_memory_ids="compact-exclude-1",
    )

    assert "compact-keep-1" in result
    assert "compact-exclude-1" not in result


@patch("foresight.server.process_session_transcript", side_effect=_mock_capture)
def test_compaction_lifecycle_respects_max_chars(_mock):
    """max_chars limits the recovery payload size."""
    conn = _get_conn()
    for i in range(5):
        _insert_memory(
            conn,
            f"compact-budget-{i}",
            f"Memory {i}: " + "X" * 400,
            scope="session",
            importance=0.9 - i * 0.1,
        )
    conn.close()

    result = compaction_lifecycle(
        session_id="compaction-budget-test",
        messages=[{"role": "user", "content": "Test."}],
        max_chars=200,
    )

    # Recovery payload portion should be within budget (allow small overhead)
    recovery_part = result.split("Recovery Context")[-1] if "Recovery Context" in result else result
    assert len(recovery_part) <= 250


@patch("foresight.server.process_session_transcript", side_effect=_mock_capture)
def test_compaction_lifecycle_returns_both_parts(_mock):
    """Result contains both the capture line and recovery payload, in order."""
    messages = [
        {"role": "user", "content": "We decided to use Redis for caching the session tokens."},
        {"role": "assistant", "content": "Understood. I'll configure Redis with a 15-minute TTL."},
    ]

    result = compaction_lifecycle(
        session_id="compaction-order-test",
        messages=messages,
    )

    # Capture confirmation comes first, then recovery payload
    capture_idx = result.find("Processed transcript")
    recovery_idx = result.find("Recovery Context")
    assert capture_idx >= 0
    assert recovery_idx >= 0
    assert capture_idx < recovery_idx
