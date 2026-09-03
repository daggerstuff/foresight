"""Tests for ForesightOAuthProvider — in-memory OAuth 2.1 server with DCR."""

from __future__ import annotations

import asyncio
import time

import pytest

from foresight.auth_oauth import ForesightOAuthProvider

from fastmcp.server.auth.auth import AccessToken, AuthorizationCode, RefreshToken
from mcp.server.auth.provider import AuthorizationParams, AuthorizeError, TokenError
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl


def _make_provider() -> ForesightOAuthProvider:
    return ForesightOAuthProvider(base_url="http://localhost:8766")


def _make_client(
    client_id: str = "test-client-1",
    client_secret: str = "super-secret-123",
    redirect_uri: str = "https://gemini.google.com/callback",
) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uris=[AnyHttpUrl(redirect_uri)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
        client_name="Test Client",
        application_type="web",
        scope=None,
        client_uri=None,
        logo_uri=None,
        contacts=None,
        tos_uri=None,
        policy_uri=None,
        jwks_uri=None,
        jwks=None,
        software_id=None,
        software_version=None,
        client_id_issued_at=None,
        client_secret_expires_at=None,
        issuer=None,
    )


def _make_auth_params(
    redirect_uri: str = "https://gemini.google.com/callback",
    state: str = "test-state",
    code_challenge: str = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
    scopes: list[str] | None = None,
) -> AuthorizationParams:
    return AuthorizationParams(
        state=state,
        scopes=scopes or [],
        code_challenge=code_challenge,
        redirect_uri=AnyHttpUrl(redirect_uri),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


# --- Client Registration (DCR) ------------------------------------------


async def test_register_and_get_client():
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    retrieved = await provider.get_client("test-client-1")
    assert retrieved is not None
    assert retrieved.client_id == "test-client-1"
    assert retrieved.client_secret == "super-secret-123"


async def test_get_client_unknown_returns_none():
    provider = _make_provider()
    result = await provider.get_client("nonexistent")
    assert result is None


# --- Authorize -----------------------------------------------------------


async def test_authorize_issues_code_and_returns_redirect():
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    params = _make_auth_params(state="xyz")
    redirect_url = await provider.authorize(client, params)
    assert "https://gemini.google.com/callback" in redirect_url
    assert "code=" in redirect_url
    assert "state=xyz" in redirect_url
    assert "iss=" in redirect_url


async def test_authorize_unknown_client_raises():
    provider = _make_provider()
    client = _make_client(client_id="unknown")
    params = _make_auth_params()
    with pytest.raises(AuthorizeError):
        await provider.authorize(client, params)


# --- Authorization Code Exchange -----------------------------------------


async def test_exchange_authorization_code_issues_tokens():
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    params = _make_auth_params(scopes=["read", "write"])
    redirect_url = await provider.authorize(client, params)
    code_str = redirect_url.split("code=")[1].split("&")[0]

    auth_code = await provider.load_authorization_code(client, code_str)
    assert auth_code is not None

    token_response = await provider.exchange_authorization_code(client, auth_code)
    assert token_response.access_token
    assert token_response.token_type == "Bearer"
    assert token_response.expires_in == 3600
    assert token_response.refresh_token
    assert "read" in (token_response.scope or "")
    assert "write" in (token_response.scope or "")


async def test_authorization_code_is_single_use():
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    params = _make_auth_params()
    redirect_url = await provider.authorize(client, params)
    code_str = redirect_url.split("code=")[1].split("&")[0]

    auth_code = await provider.load_authorization_code(client, code_str)
    await provider.exchange_authorization_code(client, auth_code)

    # Second use should fail — code consumed
    auth_code2 = await provider.load_authorization_code(client, code_str)
    assert auth_code2 is None


async def test_exchange_authorization_code_wrong_client_raises():
    provider = _make_provider()
    client_a = _make_client(client_id="client-a")
    client_b = _make_client(client_id="client-b", client_secret="other-secret")
    await provider.register_client(client_a)
    await provider.register_client(client_b)

    params = _make_auth_params()
    redirect_url = await provider.authorize(client_a, params)
    code_str = redirect_url.split("code=")[1].split("&")[0]
    auth_code = await provider.load_authorization_code(client_a, code_str)

    # Client B tries to use client A's code
    with pytest.raises(TokenError, match="invalid_grant"):
        await provider.exchange_authorization_code(client_b, auth_code)


# --- Access Token Validation ---------------------------------------------


async def test_load_access_token_valid():
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    params = _make_auth_params()
    redirect_url = await provider.authorize(client, params)
    code_str = redirect_url.split("code=")[1].split("&")[0]
    auth_code = await provider.load_authorization_code(client, code_str)
    token_response = await provider.exchange_authorization_code(client, auth_code)

    at = await provider.load_access_token(token_response.access_token)
    assert at is not None
    assert at.client_id == "test-client-1"


async def test_load_access_token_unknown_returns_none():
    provider = _make_provider()
    result = await provider.load_access_token("nonexistent-token")
    assert result is None


async def test_load_access_token_expired_returns_none():
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    params = _make_auth_params()
    redirect_url = await provider.authorize(client, params)
    code_str = redirect_url.split("code=")[1].split("&")[0]
    auth_code = await provider.load_authorization_code(client, code_str)
    token_response = await provider.exchange_authorization_code(client, auth_code)

    # Manually expire the token
    provider._access_tokens[token_response.access_token].expires_at = int(time.time()) - 1
    result = await provider.load_access_token(token_response.access_token)
    assert result is None


# --- Refresh Token Exchange ----------------------------------------------


async def test_exchange_refresh_token_issues_new_tokens():
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    params = _make_auth_params(scopes=["read"])
    redirect_url = await provider.authorize(client, params)
    code_str = redirect_url.split("code=")[1].split("&")[0]
    auth_code = await provider.load_authorization_code(client, code_str)
    token_response = await provider.exchange_authorization_code(client, auth_code)

    rt = await provider.load_refresh_token(client, token_response.refresh_token)
    assert rt is not None

    new_tokens = await provider.exchange_refresh_token(client, rt, ["read"])
    assert new_tokens.access_token
    assert new_tokens.refresh_token
    assert new_tokens.access_token != token_response.access_token
    assert new_tokens.refresh_token != token_response.refresh_token


async def test_refresh_token_rotated_after_use():
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    params = _make_auth_params()
    redirect_url = await provider.authorize(client, params)
    code_str = redirect_url.split("code=")[1].split("&")[0]
    auth_code = await provider.load_authorization_code(client, code_str)
    token_response = await provider.exchange_authorization_code(client, auth_code)

    rt = await provider.load_refresh_token(client, token_response.refresh_token)
    await provider.exchange_refresh_token(client, rt, ["read"])

    # Old refresh token should be invalidated
    rt_again = await provider.load_refresh_token(client, token_response.refresh_token)
    assert rt_again is None


# --- Revocation ----------------------------------------------------------


async def test_revoke_access_token():
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    params = _make_auth_params()
    redirect_url = await provider.authorize(client, params)
    code_str = redirect_url.split("code=")[1].split("&")[0]
    auth_code = await provider.load_authorization_code(client, code_str)
    token_response = await provider.exchange_authorization_code(client, auth_code)

    at = await provider.load_access_token(token_response.access_token)
    await provider.revoke_token(at)
    assert await provider.load_access_token(token_response.access_token) is None


async def test_revoke_refresh_token():
    provider = _make_provider()
    client = _make_client()
    await provider.register_client(client)
    params = _make_auth_params()
    redirect_url = await provider.authorize(client, params)
    code_str = redirect_url.split("code=")[1].split("&")[0]
    auth_code = await provider.load_authorization_code(client, code_str)
    token_response = await provider.exchange_authorization_code(client, auth_code)

    rt = await provider.load_refresh_token(client, token_response.refresh_token)
    await provider.revoke_token(rt)
    assert await provider.load_refresh_token(client, token_response.refresh_token) is None
