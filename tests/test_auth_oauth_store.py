"""Tests for the durable (Postgres/SQLite) OAuth state in ForesightOAuthProvider.

The production backend is Postgres; these tests use a temp SQLite file via an
explicit ``db_path`` (the documented test route through
``connection_pool.get_pool``), which exercises the same SQL surface.

Two persistence mechanisms are under test:

* **Startup rehydration** — constructing a *second* provider on the same
  ``db_path`` (the in-process analogue of a service restart) reloads clients,
  authorization codes, and tokens from the store in ``__init__``.
* **Cache-miss fallback** — a memory miss on ``get_client`` /
  ``load_access_token`` / ``load_refresh_token`` / ``load_authorization_code``
  consults the store, so state that existed before the store was reachable at
  startup is still served.

Together they guarantee that an already-authorized MCP client keeps working
across a restart.
"""

from __future__ import annotations

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyHttpUrl

from foresight.auth_oauth import MEMORY_ONLY_DB, ForesightOAuthProvider


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "oauth_test.db")


def _make_provider(db_path) -> ForesightOAuthProvider:
    return ForesightOAuthProvider(base_url="http://localhost:8767", db_path=db_path)


def _make_client(client_id: str = "persisted-client-1") -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="persisted-secret",
        redirect_uris=[AnyHttpUrl("https://example.com/callback")],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
        client_name="Persisted Client",
        application_type="web",
    )


def _make_auth_params() -> AuthorizationParams:
    return AuthorizationParams(
        state="st",
        scopes=["read"],
        code_challenge="E9Melhoa2OwwFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
        redirect_uri=AnyHttpUrl("https://example.com/callback"),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )


async def _issue_code(provider: ForesightOAuthProvider, client) -> str:
    """Run /authorize and return the issued authorization code."""
    url = await provider.authorize(client, _make_auth_params())
    return url.split("code=")[1].split("&")[0]


async def _issue_and_exchange(provider: ForesightOAuthProvider, client):
    """Issue a code and exchange it for tokens. Returns (code, token_response)."""
    code = await _issue_code(provider, client)
    auth_code = await provider.load_authorization_code(client, code)
    token_response = await provider.exchange_authorization_code(client, auth_code)
    return code, token_response


# --- Startup rehydration -------------------------------------------------


async def test_client_rehydrated_on_restart(db_path):
    provider = _make_provider(db_path)
    client = _make_client()
    await provider.register_client(client)

    # A "restarted" provider reloads the client from the store in __init__.
    restarted = _make_provider(db_path)
    assert client.client_id in restarted._clients
    retrieved = await restarted.get_client(client.client_id)
    assert retrieved is not None
    assert retrieved.client_secret == "persisted-secret"
    assert str(retrieved.redirect_uris[0]) == "https://example.com/callback"


async def test_tokens_rehydrated_on_restart(db_path):
    provider = _make_provider(db_path)
    client = _make_client()
    await provider.register_client(client)
    _code, tokens = await _issue_and_exchange(provider, client)

    restarted = _make_provider(db_path)
    # Access + refresh tokens are reloaded into memory at startup.
    assert tokens.access_token in restarted._access_tokens
    assert tokens.refresh_token in restarted._refresh_tokens
    at = await restarted.load_access_token(tokens.access_token)
    assert at is not None
    assert at.client_id == client.client_id


async def test_auth_code_rehydrated_on_restart(db_path):
    provider = _make_provider(db_path)
    client = _make_client()
    await provider.register_client(client)
    code = await _issue_code(provider, client)

    restarted = _make_provider(db_path)
    assert code in restarted._auth_codes
    # The exchange completes across the "restart".
    loaded = await restarted.load_authorization_code(client, code)
    assert loaded is not None
    token_response = await restarted.exchange_authorization_code(client, loaded)
    assert token_response.access_token
    assert token_response.refresh_token


# --- Cache-miss fallback (store consulted on a memory miss) --------------


async def test_cache_miss_falls_back_to_store(db_path):
    provider = _make_provider(db_path)
    client = _make_client()
    await provider.register_client(client)
    _code, tokens = await _issue_and_exchange(provider, client)

    # Restart, then wipe the in-memory dicts to simulate state that the store
    # holds but memory does not (e.g. created after startup reload ran).
    restarted = _make_provider(db_path)
    restarted._clients.clear()
    restarted._access_tokens.clear()
    restarted._refresh_tokens.clear()

    # Every lookup should recover from the store, not from memory.
    assert await restarted.get_client(client.client_id) is not None
    assert await restarted.load_access_token(tokens.access_token) is not None
    rt = await restarted.load_refresh_token(client, tokens.refresh_token)
    assert rt is not None


async def test_full_flow_survives_restart(db_path):
    """The end-to-end guarantee: a restarted provider can rotate the token."""
    provider = _make_provider(db_path)
    client = _make_client()
    await provider.register_client(client)
    _code, tokens = await _issue_and_exchange(provider, client)

    # Restart, then rotate the refresh token against the reloaded state.
    restarted = _make_provider(db_path)
    rt = await restarted.load_refresh_token(client, tokens.refresh_token)
    assert rt is not None
    new_tokens = await restarted.exchange_refresh_token(client, rt, ["read"])
    assert new_tokens.access_token != tokens.access_token
    # The rotated-in access token validates on yet another "restart".
    restarted_again = _make_provider(db_path)
    assert await restarted_again.load_access_token(new_tokens.access_token) is not None


# --- Degradation / isolation ---------------------------------------------


async def test_memory_only_does_not_touch_store(tmp_path):
    """The memory-only sentinel must not probe or write to any store."""
    provider = ForesightOAuthProvider(base_url="http://localhost:8768", db_path=MEMORY_ONLY_DB)
    assert provider._store_available() is False
    await provider.register_client(_make_client("mem-only-client"))
    # Works from memory; the store was never consulted.
    assert await provider.get_client("mem-only-client") is not None


async def test_store_unavailable_degrades_to_memory_only(tmp_path):
    """An unopenable store must not break construction or the OAuth flow."""
    provider = ForesightOAuthProvider(
        base_url="http://localhost:8769",
        db_path=str(tmp_path / "does-not-exist" / "nope.db"),
    )
    # __init__ degraded to memory-only (reload failed), and the flow still works.
    await provider.register_client(_make_client("fallback-client"))
    assert await provider.get_client("fallback-client") is not None
