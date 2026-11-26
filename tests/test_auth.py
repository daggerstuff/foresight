import os
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.shared.exceptions import MCPError

import foresight.auth as auth_module
from foresight import server as server_module
from foresight.auth import AuthManager, AuthMiddleware, OAuthCallerMiddleware, Role


def _tool_result_is_error(result) -> bool:
    """Auth failures must set the result's ``is_error`` flag.

    The flag has to live on the result itself, not in ``meta``: MCP clients
    read ``CallToolResult.is_error`` and otherwise see a denial as a
    successful call whose text happens to contain the denial message.
    """
    return bool(getattr(result, "is_error", False))


@pytest.fixture
def temp_db_path(tmp_path):
    # Create a temporary SQLite DB file
    db_file = tmp_path / "test_memory.db"
    # Ensure the env variable points to this file
    old_db_path = os.environ.get("FORESIGHT_DB_PATH")
    os.environ["FORESIGHT_DB_PATH"] = str(db_file)
    yield str(db_file)
    if old_db_path is not None:
        os.environ["FORESIGHT_DB_PATH"] = old_db_path
    else:
        os.environ.pop("FORESIGHT_DB_PATH", None)


def test_user_creation_and_authentication(temp_db_path):
    # Ensure fresh manager uses temp DB
    manager = AuthManager(db_path=temp_db_path)
    username = "testuser"
    email = "test@example.com"
    password = "SecretPass123!"
    role = Role.USER

    user = manager.create_user(username=username, email=email, password=password, role=role)
    assert user.username == username
    # Authentication should succeed with correct password
    auth_user = manager.authenticate_user(username=username, password=password)
    assert auth_user is not None
    assert auth_user.user_id == user.user_id
    assert not user.password_hash.startswith("sha256$")
    # Wrong password should fail
    assert manager.authenticate_user(username=username, password="wrong") is None


def test_api_key_authentication_defaults_to_required(monkeypatch):
    monkeypatch.delenv("FORESIGHT_REQUIRE_API_KEY", raising=False)
    monkeypatch.delenv("FORESIGHT_ALLOW_UNAUTHENTICATED", raising=False)

    assert auth_module._should_require_api_key() is True


def test_allow_unauthenticated_env_disables_api_key_requirement(monkeypatch):
    monkeypatch.delenv("FORESIGHT_REQUIRE_API_KEY", raising=False)
    monkeypatch.setenv("FORESIGHT_ALLOW_UNAUTHENTICATED", "true")

    assert auth_module._should_require_api_key() is False


def test_validate_session_rejects_inactive_user(temp_db_path):
    manager = AuthManager(db_path=temp_db_path)
    user = manager.create_user(
        username="sessionuser",
        email="session@example.com",
        password="SecretPass123!",
        role=Role.USER,
    )
    session_id = manager.create_session(user)

    pool = auth_module.get_pool(temp_db_path)
    conn = pool.acquire()
    try:
        conn.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user.user_id,))
        conn.commit()
    finally:
        pool.release(conn)

    assert manager.validate_session(session_id) is None


def _make_context(api_key: str, tenant_id: str):
    return SimpleNamespace(
        message=SimpleNamespace(
            arguments={"tenant_id": tenant_id},
            meta=SimpleNamespace(model_extra={"api_key": api_key}),
        )
    )


@pytest.mark.asyncio
async def test_auth_middleware_blocks_unauthorized_tenant_access(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = manager.create_user(
        username="readonly",
        email="readonly@example.com",
        password="SecretPass123!",
        role=Role.READONLY,
        tenant_access=["tenant-a"],
    )

    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    ctx = _make_context(user.api_key, "tenant-b")
    call_next = AsyncMock(return_value="ok")

    result = await AuthMiddleware().on_call_tool(ctx, call_next)

    assert _tool_result_is_error(result) is True
    assert "Tenant access denied" in result.content[0].text
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_middleware_allows_authorized_tenant_access(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = manager.create_user(
        username="readonly-ok",
        email="readonly-ok@example.com",
        password="SecretPass123!",
        role=Role.READONLY,
        tenant_access=["tenant-a"],
    )

    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    ctx = _make_context(user.api_key, "tenant-a")
    call_next = AsyncMock(return_value="ok")

    result = await AuthMiddleware().on_call_tool(ctx, call_next)

    assert result == "ok"
    call_next.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_middleware_requires_api_key_when_missing(monkeypatch):
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    ctx = SimpleNamespace(
        message=SimpleNamespace(arguments={"tenant_id": "tenant-a"}, meta=SimpleNamespace(model_extra={})),
    )
    call_next = AsyncMock(return_value="ok")

    result = await AuthMiddleware().on_call_tool(ctx, call_next)

    assert _tool_result_is_error(result) is True
    assert "missing api_key" in result.content[0].text
    call_next.assert_not_awaited()


def _make_resource_context(api_key: str = ""):
    return SimpleNamespace(
        message=SimpleNamespace(
            uri="foresight://skills",
            meta=SimpleNamespace(model_extra={"api_key": api_key}),
        )
    )


@pytest.mark.asyncio
async def test_auth_middleware_requires_api_key_for_resource_reads(monkeypatch):
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    call_next = AsyncMock(return_value="skills")

    # Resource reads must return a ResourceResult, so the denial is raised as a
    # protocol error rather than returned as a ToolResult.
    with pytest.raises(MCPError) as denied:
        await AuthMiddleware().on_read_resource(_make_resource_context(), call_next)

    assert "missing api_key" in denied.value.message
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_middleware_binds_caller_identity_for_tool_calls(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = manager.create_user(
        username="bound-caller",
        email="bound-caller@example.com",
        password="SecretPass123!",
    )

    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    seen: list[str | None] = []

    async def call_next(_context):
        seen.append(auth_module.get_caller_user_id())
        return "ok"

    result = await AuthMiddleware().on_call_tool(_make_context(user.api_key, "tenant-a"), call_next)

    assert result == "ok"
    assert seen == [user.user_id]
    assert auth_module.get_caller_user_id() is None


@pytest.mark.asyncio
async def test_auth_middleware_binds_caller_identity_for_resource_reads(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = manager.create_user(
        username="bound-reader",
        email="bound-reader@example.com",
        password="SecretPass123!",
    )

    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    seen: list[str | None] = []

    async def call_next(_context):
        seen.append(auth_module.get_caller_user_id())
        return "skills"

    result = await AuthMiddleware().on_read_resource(_make_resource_context(user.api_key), call_next)

    assert result == "skills"
    assert seen == [user.user_id]
    assert auth_module.get_caller_user_id() is None


# --- OAuth caller binding and read-only enforcement -------------------------
#
# The OAuth provider verifies a bearer token but binds no principal, so every
# tool body that reads the bound caller saw "no caller". And Role.READONLY
# existed with no enforcement at all: a read-only key could write through every
# tool.


def _make_named_tool_context(api_key: str, tenant_id: str, tool_name: str, arguments: dict | None = None):
    context = _make_context(api_key, tenant_id)
    context.message.name = tool_name
    context.message.arguments = {**context.message.arguments, **(arguments or {})}
    return context


def _bind_oauth_access_token(subject: str | None):
    access = AccessToken(
        token="oauth-access-token",
        client_id="client",
        scopes=["foresight.read"],
        subject=subject,
    )
    return auth_context_var.set(AuthenticatedUser(access))


def _reset_oauth_access_token(token) -> None:
    auth_context_var.reset(token)


@pytest.mark.asyncio
async def test_readonly_key_cannot_call_a_write_tool(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = manager.create_user(
        username="readonly-writer",
        email="readonly-writer@example.com",
        password="SecretPass123!",
        role=Role.READONLY,
        tenant_access=["tenant-a"],
    )

    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    call_next = AsyncMock(return_value="wrote")

    result = await AuthMiddleware().on_call_tool(
        _make_named_tool_context(user.api_key, "tenant-a", "record_episode_outcome"),
        call_next,
    )

    assert _tool_result_is_error(result) is True
    assert "Read-only" in result.content[0].text
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_key_can_still_read(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = manager.create_user(
        username="readonly-reader",
        email="readonly-reader@example.com",
        password="SecretPass123!",
        role=Role.READONLY,
        tenant_access=["tenant-a"],
    )

    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    call_next = AsyncMock(return_value="memories")

    result = await AuthMiddleware().on_call_tool(
        _make_named_tool_context(user.api_key, "tenant-a", "search_memories"),
        call_next,
    )

    assert result == "memories"
    call_next.assert_awaited_once()


@pytest.mark.asyncio
async def test_oauth_middleware_binds_the_token_subject_as_the_caller(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = manager.create_user(
        username="oauth-caller",
        email="oauth-caller@example.com",
        password="SecretPass123!",
        tenant_access=["tenant-a"],
    )

    monkeypatch.setattr(auth_module, "_auth_manager", manager)

    seen: list[str | None] = []

    async def call_next(_context):
        seen.append(auth_module.get_caller_user_id())
        return "ok"

    token = _bind_oauth_access_token(user.user_id)
    try:
        result = await OAuthCallerMiddleware().on_call_tool(_make_context("", "tenant-a"), call_next)
    finally:
        _reset_oauth_access_token(token)

    assert result == "ok"
    assert seen == [user.user_id]
    assert auth_module.get_caller_user_id() is None


@pytest.mark.asyncio
async def test_oauth_middleware_refuses_a_token_without_a_user(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    manager.create_user(
        username="oauth-subjectless",
        email="oauth-subjectless@example.com",
        password="SecretPass123!",
        tenant_access=["tenant-a"],
    )

    monkeypatch.setattr(auth_module, "_auth_manager", manager)

    call_next = AsyncMock(return_value="ok")

    token = _bind_oauth_access_token(None)
    try:
        result = await OAuthCallerMiddleware().on_call_tool(_make_context("", "tenant-a"), call_next)
    finally:
        _reset_oauth_access_token(token)

    assert _tool_result_is_error(result) is True
    assert "names no user" in result.content[0].text
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_oauth_middleware_applies_the_read_only_gate(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = manager.create_user(
        username="oauth-readonly",
        email="oauth-readonly@example.com",
        password="SecretPass123!",
        role=Role.READONLY,
        tenant_access=["tenant-a"],
    )

    monkeypatch.setattr(auth_module, "_auth_manager", manager)

    call_next = AsyncMock(return_value="wrote")

    token = _bind_oauth_access_token(user.user_id)
    try:
        result = await OAuthCallerMiddleware().on_call_tool(
            _make_named_tool_context("", "tenant-a", "record_episode_outcome"),
            call_next,
        )
    finally:
        _reset_oauth_access_token(token)

    assert _tool_result_is_error(result) is True
    assert "Read-only" in result.content[0].text
    call_next.assert_not_awaited()


# --- Read-only policy: mixed tools and read tools with write side effects ----


def _readonly_user(manager: AuthManager, name: str):
    return manager.create_user(
        username=f"readonly-{name}",
        email=f"readonly-{name}@example.com",
        password="SecretPass123!",
        role=Role.READONLY,
        tenant_access=["tenant-a"],
    )


def _writable_user(manager: AuthManager, name: str):
    return manager.create_user(
        username=f"writable-{name}",
        email=f"writable-{name}@example.com",
        password="SecretPass123!",
        role=Role.ADMIN,
        tenant_access=["tenant-a"],
    )


@pytest.mark.asyncio
async def test_readonly_key_can_read_a_mixed_tool(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "mixed-read")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    call_next = AsyncMock(return_value="read")

    cases = ({"params": {"action": "list"}}, {"action": "get"})
    for arguments in cases:
        result = await AuthMiddleware().on_call_tool(
            _make_named_tool_context(user.api_key, "tenant-a", "manage_context_blocks", arguments),
            call_next,
        )
        assert result == "read"

    assert call_next.await_count == len(cases)


@pytest.mark.asyncio
async def test_readonly_key_cannot_write_through_a_mixed_tool(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "mixed-write")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    call_next = AsyncMock(return_value="wrote")

    for arguments in (
        {"params": {"action": "update"}},
        {"params": {"action": "reset"}},
        {"params": {"action": "clear"}},
        {"params": {}},
        {},
    ):
        result = await AuthMiddleware().on_call_tool(
            _make_named_tool_context(user.api_key, "tenant-a", "manage_context_blocks", arguments),
            call_next,
        )
        assert _tool_result_is_error(result) is True
        assert "Read-only" in result.content[0].text

    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_mixed_tool_policy_does_not_open_a_write_only_tool(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "write-only")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    call_next = AsyncMock(return_value="stored")

    result = await AuthMiddleware().on_call_tool(
        _make_named_tool_context(user.api_key, "tenant-a", "manage_memories", {"params": {"action": "store"}}),
        call_next,
    )

    assert _tool_result_is_error(result) is True
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_key_can_synthesize_a_profile(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "synthesize")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    call_next = AsyncMock(return_value="synthesized")

    result = await AuthMiddleware().on_call_tool(
        _make_named_tool_context(user.api_key, "tenant-a", "synthesize_profile", {"params": {}}),
        call_next,
    )

    assert result == "synthesized"
    call_next.assert_awaited_once()


def test_readonly_caller_does_not_reinforce_what_it_read(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "reinforce")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)

    reinforced: list[tuple[str, str, str]] = []

    class _DecaySpy:
        def reinforce_memory(self, memory_id: str, user_id: str, tenant_id: str) -> None:
            reinforced.append((memory_id, user_id, tenant_id))

    def _decay_model() -> _DecaySpy:
        return _DecaySpy()

    monkeypatch.setattr(server_module, "get_decay_model", _decay_model)

    token = auth_module.set_caller_identity(user)
    try:
        server_module._auto_reinforce_batch(["mem-1"], user.user_id, "tenant-a")
    finally:
        auth_module.reset_caller_identity(token)

    assert reinforced == []


def test_writable_caller_still_reinforces_what_it_read(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = _writable_user(manager, "reinforce")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)

    reinforced: list[tuple[str, str, str]] = []

    class _DecaySpy:
        def reinforce_memory(self, memory_id: str, user_id: str, tenant_id: str) -> None:
            reinforced.append((memory_id, user_id, tenant_id))

    def _decay_model() -> _DecaySpy:
        return _DecaySpy()

    monkeypatch.setattr(server_module, "get_decay_model", _decay_model)

    token = auth_module.set_caller_identity(user)
    try:
        server_module._auto_reinforce_batch(["mem-1"], user.user_id, "tenant-a")
    finally:
        auth_module.reset_caller_identity(token)

    assert reinforced == [("mem-1", user.user_id, "tenant-a")]


def _capture_spies(monkeypatch, server_module) -> tuple[dict[str, int], list[tuple[object, str, str]]]:
    seen = {"store": 0, "capture": 0}
    captured: list[tuple[object, str, str]] = []

    def _store(*_args: object, **_kwargs: object) -> str:
        seen["store"] += 1
        return "stored"

    class _PipelineSpy:
        def capture_in_flight(self, target: object, user_id: str, tenant_id: str) -> list:
            seen["capture"] += 1
            captured.append((target, user_id, tenant_id))
            return []

    match = SimpleNamespace(content="remember this: tea at four", metadata={})
    monkeypatch.setattr(server_module, "_handle_memory_store", _store)

    def _pipeline_spy() -> _PipelineSpy:
        return _PipelineSpy()

    def _extract(_text: str, triggers: object) -> list:
        return [match] if triggers else []

    monkeypatch.setattr(server_module, "get_capture_pipeline", _pipeline_spy)
    monkeypatch.setattr(server_module, "extract_triggered_memories", _extract)
    return seen, captured


def test_readonly_caller_does_not_capture_in_flight(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "capture")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    seen, captured = _capture_spies(monkeypatch, server_module)

    token = auth_module.set_caller_identity(user)
    try:
        stored = server_module._auto_capture_in_flight("remember this: tea at four", user.user_id, "tenant-a")
    finally:
        auth_module.reset_caller_identity(token)

    assert stored == 0
    assert seen == {"store": 0, "capture": 0}
    assert captured == []


def test_writable_caller_still_captures_in_flight(temp_db_path, monkeypatch):
    manager = AuthManager(db_path=temp_db_path)
    user = _writable_user(manager, "capture")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    seen, captured = _capture_spies(monkeypatch, server_module)

    token = auth_module.set_caller_identity(user)
    try:
        stored = server_module._auto_capture_in_flight("remember this: tea at four", user.user_id, "tenant-a")
    finally:
        auth_module.reset_caller_identity(token)

    assert stored == 1
    assert seen == {"store": 1, "capture": 1}
    assert captured == [("remember this: tea at four", user.user_id, "tenant-a")]


def _last_login(db_path: str, user_id: str) -> str | None:
    """Read the stored last_login without going through the manager."""
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT last_login FROM users WHERE user_id = ?", (user_id,)).fetchone()
    assert row is not None
    return row[0]


def _set_last_login(db_path: str, user_id: str, value: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE users SET last_login = ? WHERE user_id = ?", (value, user_id))
        conn.commit()


@pytest.mark.parametrize("tool_name", ["synthesize_profile", "export_profile"])
def test_readonly_caller_cannot_read_another_users_profile(tool_name, temp_db_path, monkeypatch):
    """A read-only key reads its own profile and never another user's.

    Both profile tools take a caller-supplied user_id, and the tenant grant does
    not authorize that owner, so a read-only credential could read every profile
    in the tenant. Both route the requested id through _readable_user_for_caller,
    which refuses a foreign id before the store is touched.
    """
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "profile")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)

    reads: list[str] = []

    def _profile_spy(uid: str, *_args: object, **_kwargs: object) -> dict[str, list]:
        reads.append(uid)
        return {"static": [], "dynamic": []}

    def _export_spy(scope: object, *_args: object, **_kwargs: object) -> str:
        reads.append(scope.user_id)
        return "{}"

    monkeypatch.setattr(server_module, "_synthesize_profile", _profile_spy)
    monkeypatch.setattr(server_module, "_export", _export_spy)

    token = auth_module.set_caller_identity(user)
    try:
        if tool_name == "synthesize_profile":
            refused = server_module.synthesize_profile(server_module.SynthesizeProfileParams(user_id="someone-else"))
            own = server_module.synthesize_profile(server_module.SynthesizeProfileParams(user_id=user.user_id))
        else:
            refused = server_module.export_profile(user_id="someone-else")
            own = server_module.export_profile(user_id=user.user_id)
    finally:
        auth_module.reset_caller_identity(token)

    assert "only read its own data" in refused
    assert "only read its own data" not in own
    assert reads == [user.user_id]


@pytest.mark.parametrize("tool_name", ["synthesize_profile", "export_profile"])
def test_readonly_profile_read_defaults_to_the_caller(temp_db_path, monkeypatch, tool_name):
    """A read-only caller that names no user reads its own profile.

    The configured FORESIGHT_USER_ID belongs to the operator, not to a
    read-only credential, so defaulting an omitted id to it denied the ordinary
    read while an explicit own id worked - the opposite of the intent.
    """
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "profile-default")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)

    reads: list[str] = []

    def _profile_spy(uid: str, *_args: object, **_kwargs: object) -> dict[str, list]:
        reads.append(uid)
        return {"static": [], "dynamic": []}

    def _export_spy(scope: object, *_args: object, **_kwargs: object) -> str:
        reads.append(scope.user_id)
        return "{}"

    monkeypatch.setattr(server_module, "_synthesize_profile", _profile_spy)
    monkeypatch.setattr(server_module, "_export", _export_spy)

    token = auth_module.set_caller_identity(user)
    try:
        if tool_name == "synthesize_profile":
            result = server_module.synthesize_profile(server_module.SynthesizeProfileParams())
        else:
            result = server_module.export_profile()
    finally:
        auth_module.reset_caller_identity(token)

    assert "only read its own" not in result
    assert reads == [user.user_id]


def test_writable_caller_keeps_the_configured_profile_default(temp_db_path, monkeypatch):
    """The operator's default id still applies to callers that may read it."""
    manager = AuthManager(db_path=temp_db_path)
    user = _writable_user(manager, "profile-default")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)

    reads: list[str] = []

    def _profile_spy(uid: str, *_args: object, **_kwargs: object) -> dict[str, list]:
        reads.append(uid)
        return {"static": [], "dynamic": []}

    monkeypatch.setattr(server_module, "_synthesize_profile", _profile_spy)

    token = auth_module.set_caller_identity(user)
    try:
        server_module.synthesize_profile(server_module.SynthesizeProfileParams())
    finally:
        auth_module.reset_caller_identity(token)

    assert reads == [server_module.USER_ID]


@pytest.mark.asyncio
async def test_readonly_key_cannot_write_through_the_options_model(temp_db_path, monkeypatch):
    """The gate must read the action the handler dispatches.

    manage_inferred runs ``params.options.action`` whenever options is present,
    so ``{"action": "list", "options": {"action": "approve"}}`` is an approval.
    Reading only ``params.action`` let a read-only key change review state.
    """
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "options-write")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    call_next = AsyncMock(return_value="approved")

    result = await AuthMiddleware().on_call_tool(
        _make_named_tool_context(
            user.api_key,
            "tenant-a",
            "manage_inferred",
            {"params": {"action": "list", "options": {"action": "approve", "memory_id": "m1"}}},
        ),
        call_next,
    )

    assert _tool_result_is_error(result) is True
    assert "Read-only" in result.content[0].text
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_key_can_read_through_the_options_model(temp_db_path, monkeypatch):
    """An action given only in the options model is still a read."""
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "options-read")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)
    monkeypatch.setattr(auth_module, "_should_require_api_key", lambda: True)

    call_next = AsyncMock(return_value="listed")

    result = await AuthMiddleware().on_call_tool(
        _make_named_tool_context(
            user.api_key,
            "tenant-a",
            "manage_inferred",
            {"params": {"options": {"action": "list"}}},
        ),
        call_next,
    )

    assert result == "listed"
    call_next.assert_awaited_once()


def test_readonly_caller_cannot_list_another_users_inferred_queue(temp_db_path, monkeypatch):
    """The queue is a read surface; its user id is not the request's to choose."""
    manager = AuthManager(db_path=temp_db_path)
    user = _readonly_user(manager, "inferred")
    monkeypatch.setattr(auth_module, "_auth_manager", manager)

    token = auth_module.set_caller_identity(user)
    try:
        refused = server_module.manage_inferred(
            server_module.ManageInferredParams(action="list", user_id="someone-else")
        )
    finally:
        auth_module.reset_caller_identity(token)

    assert "only read its own" in refused


def test_api_key_revalidation_throttles_the_login_write(temp_db_path):
    """Per-request API-key validation records the login at most once per window.

    load_access_token re-validates a bearer API key on every OAuth MCP request,
    so an unthrottled last_login write turned ordinary read traffic into a
    database write per request.
    """
    manager = AuthManager(db_path=temp_db_path)
    user = _writable_user(manager, "throttle")

    assert manager.authenticate_api_key(user.api_key) is not None
    written = _last_login(temp_db_path, user.user_id)
    assert written is not None

    assert manager.authenticate_api_key(user.api_key) is not None
    assert _last_login(temp_db_path, user.user_id) == written

    stale = (datetime.now(UTC) - timedelta(seconds=auth_module._API_KEY_LOGIN_INTERVAL_SECONDS + 1)).isoformat()
    _set_last_login(temp_db_path, user.user_id, stale)

    assert manager.authenticate_api_key(user.api_key) is not None
    assert _last_login(temp_db_path, user.user_id) != stale
