"""Time-boxed memories must not survive into a distilled context block.

``expires_at`` is the soft deadline a policy stamps on a memory; the GC pass
purges it later. Between the deadline and the purge the row is already stale,
so the distillation queries have to filter on it exactly like ``expired_at``.
"""

import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from foresight.block_registry import initialize_default_blocks
from foresight.context_blocks import _distill_preferences, _distill_project_context


class _Block:
    """One stored block, with the ownership flag distillation reads."""

    def __init__(self, content: str, auto_generated: bool) -> None:
        self.content = content
        self.auto_generated = auto_generated


class _StubState:
    """The agent state the project-context path reads before it writes."""

    def __init__(self, blocks: dict[str, str], auto: set[str]) -> None:
        self._blocks = blocks
        self._auto = auto

    def get_block(self, label: str) -> _Block | None:
        content = self._blocks.get(label)
        return None if content is None else _Block(content, label in self._auto)


class _StubAgent:
    def __init__(self) -> None:
        self.blocks: dict[str, str] = {}
        self.auto: set[str] = set()
        self.state = _StubState(self.blocks, self.auto)

    def get_block(self, label: str) -> str | None:
        return self.blocks.get(label)

    def update_block(self, label: str, content: str, auto_generated: bool = False) -> None:
        self.blocks[label] = content
        if auto_generated:
            self.auto.add(label)

    def restore_generated_block(self, label: str, placeholder: str | None = None) -> bool:
        target = placeholder or ""
        current = (self.blocks.get(label) or "").strip()
        if label not in self.auto or current == target.strip():
            return False
        self.update_block(label, target, auto_generated=True)
        return True

    def refresh_project_context(self, new_content: str, placeholder: str | None = None) -> bool:
        current = (self.blocks.get("project_context") or "").strip()
        target = placeholder or ""
        is_placeholder = not current or (bool(target) and current == target.strip())
        if not is_placeholder and "project_context" not in self.auto:
            return False
        if current == new_content.strip():
            return False
        self.update_block("project_context", new_content, auto_generated=True)
        return True


def _make_db() -> sqlite3.Connection:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            content TEXT,
            category TEXT DEFAULT 'fact',
            scope TEXT DEFAULT 'session',
            importance REAL DEFAULT 0.5,
            is_ghost INTEGER DEFAULT 0,
            expired_at TEXT,
            expires_at TEXT,
            updated_at TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    return conn


@dataclass(frozen=True)
class _Row:
    """Non-identity columns of one seeded ``memories`` row."""

    category: str = "preference"
    scope: str = "session"
    importance: float = 0.9
    expires_at: str | None = None


def _insert(conn: sqlite3.Connection, mid: str, content: str, row: _Row | None = None) -> None:
    row = row or _Row()
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO memories (id, user_id, tenant_id, content, category, scope, importance, is_ghost, "
        "expires_at, updated_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (mid, "u1", "t1", content, row.category, row.scope, row.importance, row.expires_at, now, now),
    )
    conn.commit()


def _future() -> str:
    return (datetime.now(UTC) + timedelta(days=7)).isoformat()


def _past() -> str:
    return (datetime.now(UTC) - timedelta(days=7)).isoformat()


class TestDistillPreferencesExpiry:
    def test_expired_memory_is_not_distilled(self) -> None:
        conn = _make_db()
        try:
            _insert(conn, "live", "Always run the tests before pushing")
            _insert(conn, "stale", "Deploy on Fridays without review", _Row(expires_at=_past()))
            agent = _StubAgent()
            assert _distill_preferences(conn, agent, "u1", "t1") is True
            block = agent.get_block("user_preferences") or ""
            assert "Always run the tests before pushing" in block
            assert "Deploy on Fridays without review" not in block
        finally:
            conn.close()

    def test_unexpired_memory_is_still_distilled(self) -> None:
        conn = _make_db()
        try:
            _insert(conn, "boxed", "Keep notes in the project wiki", _Row(expires_at=_future()))
            agent = _StubAgent()
            assert _distill_preferences(conn, agent, "u1", "t1") is True
            assert "Keep notes in the project wiki" in (agent.get_block("user_preferences") or "")
        finally:
            conn.close()


class TestDistillProjectContextExpiry:
    def test_expired_decision_is_not_distilled(self) -> None:
        conn = _make_db()
        try:
            _insert(conn, "live", "The API talks to Postgres through the pool", _Row(category="decision"))
            _insert(conn, "stale", "The API talks to sqlite directly", _Row(category="decision", expires_at=_past()))
            agent = _StubAgent()
            assert _distill_project_context(conn, agent, "u1", "t1") is True
            block = agent.get_block("project_context") or ""
            assert "The API talks to Postgres through the pool" in block
            assert "The API talks to sqlite directly" not in block
        finally:
            conn.close()

    def test_registry_placeholder_is_replaced(self) -> None:
        conn = _make_db()
        try:
            _insert(conn, "arc", "Ship the maintenance job behind a flag", _Row(scope="arc"))
            agent = _StubAgent()
            schema = initialize_default_blocks().get_schema("project_context")
            assert schema is not None
            agent.blocks["project_context"] = schema.content

            assert _distill_project_context(conn, agent, "u1", "t1") is True

            assert "Ship the maintenance job behind a flag" in (agent.get_block("project_context") or "")
        finally:
            conn.close()

    def test_generated_block_is_cleared_when_sources_expire(self) -> None:
        conn = _make_db()
        try:
            _insert(conn, "stale", "The API talks to sqlite directly", _Row(category="decision", expires_at=_past()))
            agent = _StubAgent()
            agent.blocks["project_context"] = "The API talks to sqlite directly"
            agent.auto.add("project_context")

            assert _distill_project_context(conn, agent, "u1", "t1") is True

            schema = initialize_default_blocks().get_schema("project_context")
            assert schema is not None
            assert agent.get_block("project_context") == schema.content
        finally:
            conn.close()

    def test_manual_block_survives_expired_sources(self) -> None:
        conn = _make_db()
        try:
            _insert(conn, "stale", "The API talks to sqlite directly", _Row(category="decision", expires_at=_past()))
            agent = _StubAgent()
            agent.blocks["project_context"] = "Hand-written architecture notes"

            assert _distill_project_context(conn, agent, "u1", "t1") is False

            assert agent.get_block("project_context") == "Hand-written architecture notes"
        finally:
            conn.close()

    def test_unexpired_arc_is_still_distilled(self) -> None:
        conn = _make_db()
        try:
            _insert(conn, "arc", "Ship the maintenance job behind a flag", _Row(scope="arc", expires_at=_future()))
            agent = _StubAgent()
            assert _distill_project_context(conn, agent, "u1", "t1") is True
            assert "Ship the maintenance job behind a flag" in (agent.get_block("project_context") or "")
        finally:
            conn.close()
