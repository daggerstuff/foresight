import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

import foresight_mcp.auth as auth_module
from foresight_mcp.auth import AuthManager, AuthMiddleware, Role

@pytest.fixture(scope="function")
def temp_db_path(tmp_path):
    # Create a temporary SQLite DB file
    db_file = tmp_path / "test_memory.db"
    # Ensure the env variable points to this file
    os.environ["FORESIGHT_DB_PATH"] = str(db_file)
    return str(db_file)

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
    # Wrong password should fail
    assert manager.authenticate_user(username=username, password="wrong") is None


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
    monkeypatch.setattr(auth_module, "_REQUIRE_API_KEY", True)

    ctx = _make_context(user.api_key, "tenant-b")
    call_next = AsyncMock(return_value="ok")

    result = await AuthMiddleware().on_call_tool(ctx, call_next)

    assert result.isError is True
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
    monkeypatch.setattr(auth_module, "_REQUIRE_API_KEY", True)

    ctx = _make_context(user.api_key, "tenant-a")
    call_next = AsyncMock(return_value="ok")

    result = await AuthMiddleware().on_call_tool(ctx, call_next)

    assert result == "ok"
    call_next.assert_awaited_once()
