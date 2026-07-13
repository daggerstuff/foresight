"""Tests for the project_context block producer in foresight.subconscious.

PIN-3001: project_context was a dead schema (no producer) — every agent saw it
permanently empty. The ContextBlockAgent._process_user_message now routes
architectural decisions and codebase-structure mentions into the
PROJECT_CONTEXT block. These tests lock that contract and guard against the
regression where the block silently stays empty.
"""

import asyncio

import pytest

from foresight.subconscious import PROJECT_CONTEXT, ContextBlockAgent

# ====== Fixtures ======


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    """Isolate DB per test — same pattern as test_capture.py / test_server.py."""
    db_file = tmp_path / "test_project_context.db"
    monkeypatch.setenv("FORESIGHT_DB_PATH", str(db_file))

    import foresight.config as config_module
    import foresight.connection_pool as conn_pool_module
    import foresight.subconscious as subconscious_module
    from foresight.backend import SqliteBackend
    from foresight.connection_pool import reset_pool
    from foresight.server import init_db

    monkeypatch.setattr(config_module, "DB_PATH", str(db_file))
    monkeypatch.setattr(conn_pool_module, "DB_PATH", str(db_file))
    monkeypatch.setattr(subconscious_module, "DB_PATH", str(db_file))
    import foresight.server as server_module

    monkeypatch.setattr(server_module, "DB_PATH", str(db_file))
    reset_pool()

    from foresight.tenant_context import set_current_account_id, set_current_user_id

    set_current_user_id("_test_user_")
    set_current_account_id("_test_")

    backend = SqliteBackend(db_path=str(db_file))
    init_db(backend=backend)
    yield
    reset_pool()
    from foresight.tenant_context import reset_tenant_context

    reset_tenant_context()


def _process(agent: ContextBlockAgent, session_id: str, messages):
    """Run the async process_transcript in a fresh event loop."""
    asyncio.run(agent.process_transcript(session_id, messages))


def _project_context_content(agent: ContextBlockAgent) -> str:
    block = agent.state.get_block(PROJECT_CONTEXT)
    assert block is not None, "project_context block must exist after initialize_defaults"
    return block.content


def _project_context_is_empty(agent: ContextBlockAgent) -> bool:
    """Empty state is the '(No ...)' sentinel, not a blank string."""
    block = agent.state.get_block(PROJECT_CONTEXT)
    assert block is not None
    return block.is_empty()


# ====== Producer tests ======


def test_decision_phrase_routes_to_project_context():
    """A stated architectural decision lands in project_context."""
    agent = ContextBlockAgent()
    msg = "We decided to migrate the ingestion gate from stdio to streamable-http transport."
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    content = _project_context_content(agent)
    assert "migrate" in content.lower()
    assert msg[:50] in content  # snippet preserved


def test_file_extension_mention_routes_to_project_context():
    """Mentioning a source file path lands in project_context."""
    agent = ContextBlockAgent()
    msg = "The server entry is foresight/server.py and the CLI lives in cli/main.py."
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    content = _project_context_content(agent)
    assert "server.py" in content


def test_path_like_token_routes_to_project_context():
    """A dir/dir token mention lands in project_context even without file ext."""
    agent = ContextBlockAgent()
    msg = "The CLI lives in foresight/cli and the server entry is in foresight/server directory."
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    content = _project_context_content(agent)
    assert "foresight/cli" in content


def test_pure_preference_does_not_land_in_project_context():
    """A pure preference message routes to user_preferences, NOT project_context."""
    agent = ContextBlockAgent()
    msg = "I prefer pnpm over npm always."
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    content = _project_context_content(agent)
    assert "pnpm" not in content, "preference leaked into project_context"


def test_pure_todo_does_not_land_in_project_context():
    """A pure TODO routes to pending_items, NOT project_context."""
    agent = ContextBlockAgent()
    msg = "TODO: ship the redis companion next week."
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    content = _project_context_content(agent)
    assert "redis" not in content, "pending item leaked into project_context"


def test_benign_message_leaves_block_empty():
    """A benign conversational message routes to nothing — block stays empty."""
    agent = ContextBlockAgent()
    msg = "Hello, how are you today?"
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    assert _project_context_is_empty(agent), (
        f"benign message wrongly wrote project_context: {_project_context_content(agent)!r}"
    )


def test_multiple_decisions_accumulate():
    """Two separate decisions both append — block grows, not overwrites."""
    agent = ContextBlockAgent()
    _process(
        agent,
        "sess-1",
        [
            {"role": "user", "content": "We decided to rename the config module to settings."},
            {"role": "user", "content": "We refactored the server into a thin entry point."},
        ],
    )

    content = _project_context_content(agent)
    # Both snippets present (append strategy, not overwrite)
    assert "rename" in content.lower()
    assert "refactored" in content.lower()
    # Two timestamped entries
    assert content.count("\n- [") >= 2 or content.count("- [") >= 2


def test_assistant_messages_are_ignored_for_project_context():
    """Only user messages feed the producer — assistant turns must not."""
    agent = ContextBlockAgent()
    _process(
        agent,
        "sess-1",
        [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "We decided to migrate the auth layer to OAuth2."},
        ],
    )

    assert _project_context_is_empty(agent), "assistant message wrongly wrote project_context"


def test_snippet_truncated_to_200_chars():
    """Long decision content is trimmed so blocks stay compact."""
    agent = ContextBlockAgent()
    long_tail = "z" * 500
    # Strong verb + a technical object (the "service"/"layer"/"queue" stack nouns)
    # so the message still routes through the tightened heuristic and exercises
    # the 200-char snippet trim.
    msg = f"We decided to migrate the service layer to a new queue {long_tail}."
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    content = _project_context_content(agent)
    # find the appended line's payload
    marker = "- ["
    idx = content.find(marker)
    assert idx >= 0
    appended_line = content[idx:]
    # Line shape: "- [<ts>] (session: <sid>) <snippet>"
    # Strip timestamp "- [..] " then the "(session: ..) " metadata to isolate the snippet.
    after_ts = appended_line[appended_line.find("] ") + 2 :]
    session_close = after_ts.find(") ")
    assert session_close >= 0, f"session metadata missing in line: {after_ts!r}"
    snippet = after_ts[session_close + 2 :]
    assert len(snippet) <= 200, f"snippet not trimmed: len={len(snippet)}"


# ====== Tightened-heuristic + source-attribution tests ======


def test_bare_soft_phrase_rejected():
    """Bare 'we use the red button' must NOT route — no technical object present.

    Guards against noise: the soft phrase 'we use' alone matches ordinary English.
    """
    agent = ContextBlockAgent()
    msg = "We use the red button on the left side of the page."
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    assert _project_context_is_empty(agent), (
        f"bare soft phrase wrongly wrote project_context: {_project_context_content(agent)!r}"
    )


def test_non_technical_decision_does_not_pollute_project_context():
    """A strong verb without a code/architecture cue must NOT route.

    Regression for ordinary decisions like "I decided to migrate to another
    city" — they used to qualify on the strong verb alone and pollute the
    project_context block.
    """
    agent = ContextBlockAgent()
    msg = "I decided to migrate to another city next month."
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    assert _project_context_is_empty(agent), (
        f"non-technical decision wrongly wrote project_context: "
        f"{_project_context_content(agent)!r}"
    )


def test_soft_phrase_with_stack_noun_routes():
    """'we use streamable-http transport' SHOULD route — stack noun qualifies it."""
    agent = ContextBlockAgent()
    msg = "We use streamable-http transport for the MCP server."
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    content = _project_context_content(agent)
    assert "streamable-http" in content


def test_bare_path_fact_routes_without_verb():
    """A bare source path mention routes as a codebase fact — no verb needed."""
    agent = ContextBlockAgent()
    msg = "src/api/users.py"
    _process(agent, "sess-1", [{"role": "user", "content": msg}])

    content = _project_context_content(agent)
    assert "src/api/users.py" in content


def test_session_id_attribution_in_block_entry():
    """The producing session_id is embedded in the appended line for traceability."""
    agent = ContextBlockAgent()
    msg = "We decided to rename the config module to settings."
    _process(agent, "sess-trace-42", [{"role": "user", "content": msg}])

    content = _project_context_content(agent)
    assert "(session: sess-trace-42)" in content, (
        f"session attribution missing in block entry: {content!r}"
    )


def test_distinct_sessions_produce_distinct_session_tags():
    """Two decisions from two sessions tag their origins separately."""
    agent = ContextBlockAgent()
    _process(
        agent,
        "sess-A",
        [{"role": "user", "content": "We decided to split the registry into shards."}],
    )
    _process(
        agent,
        "sess-B",
        [{"role": "user", "content": "We migrated the queue to redis."}],
    )

    content = _project_context_content(agent)
    assert "(session: sess-A)" in content
    assert "(session: sess-B)" in content
