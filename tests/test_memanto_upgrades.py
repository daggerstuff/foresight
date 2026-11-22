"""Tests for the Memanto-inspired upgrades: attribution, catch-up, sync, expiry."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from foresight import activity_log, client_attribution, instruction_sync, retention_policy, server
from foresight.client_attribution import (
    UNKNOWN_TOOL,
    client_name_from_fastmcp_context,
    resolve_source_tool,
    seed_request_client,
)
from foresight.retention_policy import (
    RetentionConfig,
    apply_expiry_policies,
    expire_memory,
    policy_decision,
    preset_policy,
    restore_memory,
    save_policy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _memories_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            tenant_id TEXT,
            category TEXT,
            tags TEXT,
            source_tool TEXT,
            importance REAL,
            current_strength REAL,
            updated_at TEXT,
            created_at TEXT,
            is_ghost INTEGER DEFAULT 0,
            expired_at TEXT,
            expired_by TEXT,
            content TEXT,
            project TEXT
        )"""
    )


def _policies_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE memory_policies (
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            preset TEXT,
            policy_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, user_id)
        )"""
    )


def _insert_memory(conn: sqlite3.Connection, mid: str, **fields: object) -> None:
    now = datetime.now(UTC).isoformat()
    row = {
        "user_id": "u1",
        "tenant_id": "t1",
        "category": "fact",
        "tags": "[]",
        "source_tool": None,
        "importance": 0.5,
        "current_strength": None,
        "updated_at": now,
        "created_at": now,
        "is_ghost": 0,
        "expired_at": None,
        "expired_by": None,
        "project": "default",
        "content": f"content-{mid}",
    }
    row.update(fields)
    conn.execute(
        "INSERT INTO memories (id, user_id, tenant_id, category, tags, source_tool, importance, "
        "current_strength, updated_at, created_at, is_ghost, expired_at, expired_by, content, project) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mid, *[row[k] for k in (
            "user_id", "tenant_id", "category", "tags", "source_tool", "importance",
            "current_strength", "updated_at", "created_at", "is_ghost", "expired_at",
            "expired_by", "content", "project",
        )]),
    )


@pytest.fixture
def sqlite_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _memories_table(conn)
    _policies_table(conn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Isolate env markers, FORESIGHT_CLIENT, and the on-disk state dir."""
    monkeypatch.delenv("FORESIGHT_CLIENT", raising=False)
    prefixes = (
        "CLAUDECODE",
        "CLAUDE_CODE_",
        "CURSOR_",
        "CODEX_",
        "GEMINI_",
        "COPILOT_",
        "AUGMENT_",
        "WINDSURF_",
        "OPENCODE_",
    )
    for var in list(os.environ):
        if var == "CODEX_CLI" or var.startswith(prefixes):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FORESIGHT_HOME", str(tmp_path / "foresight-home"))


# ---------------------------------------------------------------------------
# Attribution precedence
# ---------------------------------------------------------------------------


class _FakeParams:
    def __init__(self, name):
        self.clientInfo = type("Info", (), {"name": name})()


class _FakeSession:
    def __init__(self, name):
        self.client_params = _FakeParams(name)


class _FakeRequestCtx:
    def __init__(self, name):
        self.session = _FakeSession(name)


class _FakeCtx:
    def __init__(self, name):
        self.request_context = _FakeRequestCtx(name)


def test_client_name_from_fastmcp_context():
    assert client_name_from_fastmcp_context(_FakeCtx("cursor")) == "cursor"
    assert client_name_from_fastmcp_context(None) is None
    assert client_name_from_fastmcp_context(object()) is None


def test_resolve_source_tool_explicit_wins(monkeypatch):
    monkeypatch.setenv("FORESIGHT_CLIENT", "cursor")
    assert resolve_source_tool("my-tool") == "my-tool"


def test_resolve_source_tool_env_client_second(monkeypatch):
    monkeypatch.setenv("FORESIGHT_CLIENT", "cursor")
    assert resolve_source_tool() == "cursor"


def test_resolve_source_tool_handshake_third():
    token = seed_request_client("claude-code")
    try:
        assert resolve_source_tool() == "claude-code"
    finally:
        client_attribution._request_client_name.reset(token)


def test_resolve_source_tool_env_markers_fourth(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert resolve_source_tool() == "claude-code"


def test_resolve_source_tool_unknown_fallback():
    assert resolve_source_tool() == UNKNOWN_TOOL


# ---------------------------------------------------------------------------
# Retention policy evaluation
# ---------------------------------------------------------------------------


def _row(**overrides):
    row = {
        "category": "fact",
        "tags": "[]",
        "source_tool": None,
        "importance": 0.5,
        "current_strength": None,
        "updated_at": (datetime.now(UTC) - timedelta(days=10)).isoformat(),
        "created_at": (datetime.now(UTC) - timedelta(days=10)).isoformat(),
    }
    row.update(overrides)
    return row


def test_policy_first_match_wins():
    config = RetentionConfig.from_dict(
        {
            "rules": [
                {"name": "pin-facts", "match": {"category": "fact"}, "expire_after": "never"},
                {"name": "expire-old", "match": {}, "expire_after": "7d"},
            ]
        }
    )
    # fact rows are pinned by the first rule even though rule 2 would expire them
    assert policy_decision(config, _row(category="fact")) is None
    assert policy_decision(config, _row(category="note")) == "policy:expire-old"


def test_policy_never_pins_against_retention_fallback():
    config = RetentionConfig.from_dict(
        {
            "retention": {"fact": "7d"},
            "rules": [{"name": "pin", "match": {"category": "fact"}, "expire_after": "never"}],
        }
    )
    assert policy_decision(config, _row(category="fact")) is None


def test_policy_retention_fallback_and_updated_at_clock():
    config = RetentionConfig.from_dict({"retention": {"pending": "7d"}})
    old = _row(category="pending", updated_at=(datetime.now(UTC) - timedelta(days=8)).isoformat())
    fresh = _row(category="pending", updated_at=datetime.now(UTC).isoformat())
    assert policy_decision(config, old) == "retention:pending"
    assert policy_decision(config, fresh) is None


def test_policy_match_fields():
    config = RetentionConfig.from_dict(
        {
            "rules": [
                {
                    "name": "weak-cursor",
                    "match": {"source_tool": "cursor", "confidence_below": 0.4, "tags": ["x"]},
                    "expire_after": "30m",
                }
            ]
        }
    )
    matching = _row(
        source_tool="cursor",
        current_strength=0.2,
        tags='["x"]',
        updated_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    assert policy_decision(config, matching) == "policy:weak-cursor"
    # confidence too high -> no match
    assert policy_decision(config, _row(source_tool="cursor", current_strength=0.9, tags='["x"]')) is None
    # wrong tool -> no match
    assert policy_decision(config, _row(source_tool="other", current_strength=0.2, tags='["x"]')) is None


def test_policy_duration_validation():
    with pytest.raises(retention_policy.PolicyError):
        RetentionConfig.from_dict({"rules": [{"name": "r", "expire_after": "1h"}]})
    for dur in ("30m", "12h", "7d", "2w", "3mo", "1y", "never"):
        RetentionConfig.from_dict({"rules": [{"name": "r", "expire_after": dur}]})


def test_presets_exist_and_validate():
    for name in ("conservative", "balanced", "aggressive"):
        RetentionConfig.from_dict(preset_policy(name))
    with pytest.raises(retention_policy.PolicyError):
        preset_policy("bogus")


# ---------------------------------------------------------------------------
# Expire / restore round trip + purge gating (sqlite)
# ---------------------------------------------------------------------------


def test_expire_restore_round_trip(sqlite_conn):
    _insert_memory(sqlite_conn, "m1")
    assert expire_memory(sqlite_conn, "m1", "u1", "t1", expired_by="manual")
    row = sqlite_conn.execute("SELECT expired_at, expired_by FROM memories WHERE id='m1'").fetchone()
    assert row["expired_at"]
    assert row["expired_by"] == "manual"
    # idempotent: second expire is a no-op
    assert not expire_memory(sqlite_conn, "m1", "u1", "t1")
    assert restore_memory(sqlite_conn, "m1", "u1", "t1")
    row = sqlite_conn.execute("SELECT expired_at, expired_by FROM memories WHERE id='m1'").fetchone()
    assert row["expired_at"] is None
    assert row["expired_by"] is None
    # restore on live memory is a no-op
    assert not restore_memory(sqlite_conn, "m1", "u1", "t1")


MEMORIES_SEEDED = 3


def test_apply_expiry_policies_first_match_and_purge_gate(sqlite_conn):
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    _insert_memory(sqlite_conn, "old-fact", category="fact", updated_at=old)
    _insert_memory(sqlite_conn, "old-note", category="note", updated_at=old)
    _insert_memory(
        sqlite_conn,
        "ghost",
        category="note",
        updated_at=old,
        is_ghost=1,
    )

    policy = {
        "retention": {"note": "7d"},
        "rules": [{"name": "pin-facts", "match": {"category": "fact"}, "expire_after": "never"}],
        "purge_expired_after": "never",
    }
    save_policy(sqlite_conn, "t1", "u1", policy)
    result = apply_expiry_policies(sqlite_conn, "t1")
    assert result == {"expired": 1, "purged": 0}

    rows = {
        r["id"]: r
        for r in sqlite_conn.execute("SELECT id, expired_at, expired_by FROM memories").fetchall()
    }
    assert rows["old-fact"]["expired_at"] is None  # pinned
    assert rows["old-note"]["expired_by"] == "retention:note"
    assert rows["ghost"]["expired_at"] is None  # ghosts untouched
    assert rows["old-note"]["expired_at"] is not None

    # purge_expired_after still 'never' -> nothing deleted even though expired
    count = sqlite_conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    assert count == MEMORIES_SEEDED

    # now allow purging and re-run: expired row older than window is deleted
    save_policy(sqlite_conn, "t1", "u1", {**policy, "purge_expired_after": "7d"})
    # expired_at was stamped 'now', so it is NOT older than 7d -> still gated
    result = apply_expiry_policies(sqlite_conn, "t1")
    assert result["purged"] == 0
    sqlite_conn.execute("UPDATE memories SET expired_at = ? WHERE id = 'old-note'", (old,))
    result = apply_expiry_policies(sqlite_conn, "t1")
    assert result["purged"] == 1
    remaining = {r["id"] for r in sqlite_conn.execute("SELECT id FROM memories").fetchall()}
    assert remaining == {"old-fact", "ghost"}


def test_apply_expiry_policies_no_policy_is_noop(sqlite_conn):
    _insert_memory(sqlite_conn, "m1")
    assert apply_expiry_policies(sqlite_conn, "t1") == {"expired": 0, "purged": 0}


# ---------------------------------------------------------------------------
# Catch-up cursor semantics (server tool, sqlite-backed)
# ---------------------------------------------------------------------------


def _catchup_conn(tmp_path):
    db = tmp_path / "catchup.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT, content TEXT,
            category TEXT, source_tool TEXT, created_at TEXT, project TEXT,
            is_ghost INTEGER DEFAULT 0, expired_at TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return db


def test_get_catch_up_cursor_semantics(monkeypatch, tmp_path):
    db = _catchup_conn(tmp_path)

    def _connect():
        c = sqlite3.connect(str(db))
        c.row_factory = sqlite3.Row
        return c

    conn = _connect()
    monkeypatch.setattr(server, "get_db_connection", _connect)
    monkeypatch.setattr(server, "get_current_account_id", lambda: "t1")

    t0 = datetime.now(UTC)
    conn.execute(
        "INSERT INTO memories VALUES ('old','u1','t1','old mem','fact','cursor',?,'default',0,NULL)",
        ((t0 - timedelta(hours=1)).isoformat(),),
    )
    conn.commit()

    # first call initializes cursor and returns empty
    first = server.get_catch_up(project="default", tool="cursor", user_id="u1")
    assert "initialized" in first.lower() or "no prior cursor" in first.lower()

    # store a new memory after cursor
    conn.execute(
        "INSERT INTO memories VALUES ('new','u1','t1','fresh mem','decision','claude-code',?,'default',0,NULL)",
        (datetime.now(UTC).isoformat(),),
    )
    conn.commit()

    second = server.get_catch_up(project="default", tool="cursor", user_id="u1")
    assert "- [DECISION] fresh mem (from claude-code)" in second
    assert "old mem" not in second

    # cursor advanced: third call returns nothing new
    third = server.get_catch_up(project="default", tool="cursor", user_id="u1")
    assert "fresh mem" not in third

    # different project lane has its own cursor -> first-run again
    other = server.get_catch_up(project="other", tool="cursor", user_id="u1")
    assert "initialized" in other.lower() or "no prior cursor" in other.lower()

    conn.close()


# ---------------------------------------------------------------------------
# Instruction-file sync
# ---------------------------------------------------------------------------


def _mem(mid, category="preference", content="likes tests", importance=0.9):
    return {"id": mid, "content": content, "category": category, "importance": importance}


def test_sync_sentinel_replacement_preserves_outside(tmp_path):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text(
        "# Title\n\nbefore\n\n"
        "<!-- FORESIGHT-DYNAMIC-MEMORIES -->\n- [OLD] stale\n<!-- /FORESIGHT-DYNAMIC-MEMORIES -->\n\n"
        "after\n"
    )
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Agents\n\nno sentinels here\n")

    report = instruction_sync.run_sync(
        tmp_path, "u1", "t1", memories=[_mem("m1", content="prefers terse code")]
    )

    text = claude.read_text()
    assert "before" in text
    assert "after" in text
    assert "# Title" in text
    assert "- [OLD] stale" not in text
    expected_block = (
        "<!-- FORESIGHT-DYNAMIC-MEMORIES -->\n"
        "- [PREFERENCE] prefers terse code\n"
        "<!-- /FORESIGHT-DYNAMIC-MEMORIES -->"
    )
    assert expected_block in text

    skipped = {s["file"]: s for s in report["skipped"]}
    assert skipped["AGENTS.md"]["reason"] == "missing-sentinels"
    assert "FORESIGHT-DYNAMIC-MEMORIES" in skipped["AGENTS.md"]["hint"]

    updated = {f["file"]: f["status"] for f in report["files"]}
    assert updated.get("CLAUDE.md") == "updated"


def test_sync_no_files_reports_empty(tmp_path):
    report = instruction_sync.run_sync(tmp_path, "u1", "t1", memories=[])
    assert report["memories"] == 0
    assert report["files"] == []
    assert report["skipped"] == []


def test_sync_second_run_unchanged(tmp_path):
    f = tmp_path / "GEMINI.md"
    f.write_text("x\n<!-- FORESIGHT-DYNAMIC-MEMORIES -->\n<!-- /FORESIGHT-DYNAMIC-MEMORIES -->\n")
    mems = [_mem("m1")]
    instruction_sync.run_sync(tmp_path, "u1", "t1", memories=mems)
    report = instruction_sync.run_sync(tmp_path, "u1", "t1", memories=mems)
    assert report["files"][0]["status"] == "unchanged"


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------


def test_activity_log_counts_only_and_prunes(monkeypatch, tmp_path):
    home = tmp_path / "fh"
    monkeypatch.setenv("FORESIGHT_HOME", str(home))
    activity_log.record_activity("memory.store", n=1, tool="cursor", session="s1")
    today = datetime.now(UTC).date().isoformat()
    path = home / "activity" / f"events-{today}.jsonl"
    assert path.exists()
    event = json.loads(path.read_text().strip())
    assert event["op"] == "memory.store"
    assert event["n"] == 1
    assert event["tool"] == "cursor"
    assert event["session"] == "s1"
    assert set(event) == {"ts", "tool", "session", "op", "n"}

    # old file gets pruned on next write
    old_dir = home / "activity"
    (old_dir / "events-2000-01-01.jsonl").write_text("{}\n")
    activity_log.record_activity("search", n=1, tool="unknown", session=None)
    assert not (old_dir / "events-2000-01-01.jsonl").exists()


def test_activity_log_failure_never_raises(monkeypatch, tmp_path):
    # Point FORESIGHT_HOME at a path that cannot be created (file in the way)
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("FORESIGHT_HOME", str(blocker / "home"))
    activity_log.record_activity("memory.store", n=1)  # must not raise
