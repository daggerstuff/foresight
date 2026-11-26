import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

import foresight.auth as auth_module
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

    result = await AuthMiddleware().on_read_resource(_make_resource_context(), call_next)

    text = result if isinstance(result, str) else "".join(chunk.text for chunk in result.content)
    assert "missing api_key" in text
    assert _tool_result_is_error(result) is True
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


def _make_named_tool_context(api_key: str, tenant_id: str, tool_name: str):
    context = _make_context(api_key, tenant_id)
    context.message.name = tool_name
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
