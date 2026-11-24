"""Tests for the `foresight retention` command group."""

from __future__ import annotations

from typing import Any

import pytest

from foresight.retention_policy import RetentionConfig
from foresight_cli.commands import retention

USAGE_ERROR_EXIT = 2


class _DummyCursor:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row


class _DummyConn:
    def __init__(self, row: Any = None, query_rows: dict[str, Any] | None = None) -> None:
        self._row = row
        self._query_rows = query_rows or {}

    def execute(self, sql: str, _params: Any = ()) -> _DummyCursor:
        for key, r in self._query_rows.items():
            if key in sql:
                return _DummyCursor(r)
        return _DummyCursor(self._row)

    def close(self) -> None:
        pass


@pytest.fixture
def retention_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    saved: dict[str, Any] = {}

    def fake_save(_conn: Any, _tenant: str, _uid: str, policy: dict, preset: str | None = None) -> RetentionConfig:
        saved.update(policy)
        saved["_preset"] = preset
        return RetentionConfig.from_dict(policy)

    monkeypatch.setattr(retention, "_init_backend", lambda: None)
    monkeypatch.setattr(retention.cfg, "get_user_id", lambda override=None: override or "u1")
    monkeypatch.setattr(retention, "get_current_account_id", lambda: "t1")
    monkeypatch.setattr(retention, "get_db_connection", _DummyConn)
    monkeypatch.setattr(retention, "save_policy", fake_save)
    return saved


def test_purge_window_requires_yes(retention_env: dict[str, Any]) -> None:
    with pytest.raises(SystemExit) as exc:
        retention.set_policy(retention.RetentionSetParams(preset="aggressive"))
    assert exc.value.code == USAGE_ERROR_EXIT
    assert retention_env == {}

    retention.set_policy(retention.RetentionSetParams(preset="aggressive", yes=True))
    assert retention_env["purge_expired_after"] == "3mo"


def test_never_purge_needs_no_confirmation(retention_env: dict[str, Any]) -> None:
    retention.set_policy(retention.RetentionSetParams(preset="conservative"))
    assert retention_env["purge_expired_after"] == "never"


def test_unknown_tenant_is_rejected(retention_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retention, "get_db_connection", lambda: _DummyConn(row=None))

    with pytest.raises(SystemExit) as exc:
        retention.set_policy(retention.RetentionSetParams(preset="conservative", tenant_id="ghost-tenant"))

    assert exc.value.code == USAGE_ERROR_EXIT
    assert retention_env == {}


def test_known_tenant_with_memories_is_accepted(retention_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    captured_params: list[tuple[str, Any]] = []

    class _TrackingConn(_DummyConn):
        def execute(self, sql: str, params: Any = ()) -> _DummyCursor:
            captured_params.append((sql, params))
            return super().execute(sql, params)

    monkeypatch.setattr(
        retention,
        "get_db_connection",
        lambda: _TrackingConn(query_rows={"FROM tenants": None, "FROM memories": (1,)}),
    )

    retention.set_policy(retention.RetentionSetParams(preset="conservative", tenant_id="live-tenant", user_id="alice"))

    assert retention_env["_preset"] == "conservative"
    # Verify the fallback queried memories with the explicit user_id "alice"
    mem_queries = [p for s, p in captured_params if "FROM memories" in s]
    assert len(mem_queries) == 1
    assert mem_queries[0] == ("live-tenant", "alice")


def test_non_object_policy_is_rejected(retention_env: dict[str, Any]) -> None:
    with pytest.raises(SystemExit) as exc:
        retention.set_policy(retention.RetentionSetParams(policy="[]", yes=True))
    assert exc.value.code == USAGE_ERROR_EXIT
    assert retention_env == {}
