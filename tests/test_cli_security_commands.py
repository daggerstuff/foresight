"""Regression tests for post-commit reporting in the security CLI commands.

``encrypt-all`` and ``rotate-key`` persist their work before they report it, so
the report must not raise: an ``AttributeError`` there tells the operator the
run failed when the data is already changed.
"""

from __future__ import annotations

from io import StringIO
from typing import Any
from unittest.mock import patch

from rich.console import Console

from foresight_cli.commands import security as security_commands
from foresight_cli.utils import output as out


class _FakeCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _FakeConnection:
    """Records the writes and transaction boundaries the commands perform."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.updates: list[tuple[str, tuple[Any, ...]]] = []
        self.committed = False
        self.closed = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> _FakeCursor:
        if sql.lstrip().upper().startswith("SELECT"):
            return _FakeCursor(self._rows)
        self.updates.append((sql, params))
        return _FakeCursor([])

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


class _FakeEncryptionEngine:
    enabled = True

    def __init__(self) -> None:
        self.encrypted: list[tuple[str, str, str, bool]] = []

    def is_encrypted(self, content: str) -> bool:
        return content.startswith("enc:v1:")

    def encrypt(
        self,
        plaintext: str,
        tenant_id: str = "default",
        user_id: str = "default",
        force: bool = False,
    ) -> str:
        self.encrypted.append((plaintext, tenant_id, user_id, force))
        return f"enc:v1:{plaintext}"


class _FakeRotationEngine(_FakeEncryptionEngine):
    def __init__(self, rotated_count: int, failed_count: int) -> None:
        super().__init__()
        self.result = {"rotated_count": rotated_count, "failed_count": failed_count}
        self.rotation: tuple[str, str, str, str] | None = None
        self.rotation_conn: _FakeConnection | None = None

    def rotate_key(
        self,
        old_master_key: str,
        new_master_key: str,
        conn: _FakeConnection,
        tenant_id: str = "default",
        user_id: str = "default",
    ) -> dict[str, Any]:
        self.rotation = (old_master_key, new_master_key, tenant_id, user_id)
        self.rotation_conn = conn
        return self.result


def test_encrypt_all_reports_success_after_the_commit() -> None:
    conn = _FakeConnection([{"id": "m1", "content": "plain"}, {"id": "m2", "content": "enc:v1:cipher"}])
    engine = _FakeEncryptionEngine()
    stderr = StringIO()

    with (
        patch.object(security_commands, "_init_backend"),
        patch.object(security_commands, "get_db_connection", return_value=conn),
        patch.object(security_commands, "get_encryption_engine", return_value=engine),
        patch.object(security_commands.cfg, "get_user_id", return_value="test-user"),
        patch.object(out, "_stderr_console", Console(file=stderr, force_terminal=False, color_system=None)),
    ):
        out.configure(mode="human", color=False, pipe_safe=False, verbose=False)
        security_commands.encrypt_all(security_commands.SecurityEncryptAllParams(user_id="test-user"))

    assert conn.committed is True
    assert conn.closed is True
    assert len(conn.updates) == 1
    assert engine.encrypted == [("plain", "default", "test-user", True)]
    rendered = stderr.getvalue()
    assert "Encrypted 1 memories" in rendered
    assert "Already encrypted: 1" in rendered


def test_rotate_key_reports_success_and_failures_after_the_rotation() -> None:
    conn = _FakeConnection([])
    engine = _FakeRotationEngine(rotated_count=3, failed_count=1)
    stderr = StringIO()

    with (
        patch.object(security_commands, "_init_backend"),
        patch.object(security_commands, "get_db_connection", return_value=conn),
        patch.object(security_commands, "get_encryption_engine", return_value=engine),
        patch.object(security_commands.cfg, "get_user_id", return_value="test-user"),
        patch.object(out, "_stderr_console", Console(file=stderr, force_terminal=False, color_system=None)),
    ):
        out.configure(mode="human", color=False, pipe_safe=False, verbose=False)
        security_commands.rotate_key(
            security_commands.SecurityRotateKeyParams(old_key="old-key", new_key="new-key", user_id="test-user")
        )

    assert engine.rotation == ("old-key", "new-key", "default", "test-user")
    assert engine.rotation_conn is conn
    assert conn.closed is True
    rendered = stderr.getvalue()
    assert "Key rotation complete: 3 memories" in rendered
    assert "Failed to decrypt 1 memories" in rendered
