"""Foresight OAuth 2.1 authorization server with Dynamic Client Registration.

Provides a self-contained OAuth provider for FastMCP that supports the full
MCP authorization flow used by cloud-hosted MCP clients like Gemini Spark:

    1. Client discovers auth endpoints via ``/.well-known/oauth-authorization-server``
    2. Client registers itself via Dynamic Client Registration (``/register``)
    3. Client requests authorization via ``/authorize`` (auto-approved)
    4. Client exchanges the authorization code for tokens via ``/token``
    5. Client calls MCP tools with ``Authorization: Bearer <access_token>``

All state (clients, codes, tokens) is kept in-memory. A server restart
invalidates everything; clients must re-register and re-authorize. This is
acceptable for a single-instance MCP server.

For multi-instance deployments, replace the in-memory dicts with a shared
store (Redis, Postgres).
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import timedelta
from urllib.parse import urlencode, urlparse

from fastmcp.server.auth import OAuthProvider
from fastmcp.server.auth.auth import (
    AccessToken,
    AuthorizationCode,
    ClientRegistrationOptions,
    RefreshToken,
    RevocationOptions,
)
from mcp.server.auth.provider import AuthorizationParams
from mcp.server.auth.provider import AuthorizeError, RegistrationError, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

# --- Constants ------------------------------------------------------------

# Access tokens: 1 hour (standard for MCP clients that cache tokens)
ACCESS_TOKEN_TTL = 3600  # seconds
# Refresh tokens: 30 days
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # seconds
# Authorization codes: 10 minutes (RFC 6749 recommends max 10 min)
AUTH_CODE_TTL = 600  # seconds


class ForesightOAuthProvider(OAuthProvider):
    """In-memory OAuth 2.1 authorization server with Dynamic Client Registration.

    Auto-approves all authorization requests (no consent screen). Designed for
    personal MCP servers where the user controls both the server and the client.

    Usage::

        from foresight.auth_oauth import ForesightOAuthProvider

        provider = ForesightOAuthProvider(
            base_url="https://foresight.example.com",
        )
        mcp = FastMCP("Foresight", auth=provider, ...)
    """

    def __init__(
        self,
        *,
        base_url: str,
        resource_base_url: str | None = None,
        issuer_url: str | None = None,
        service_documentation_url: str | None = None,
        required_scopes: list[str] | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url,
            resource_base_url=resource_base_url,
            issuer_url=issuer_url,
            service_documentation_url=service_documentation_url,
            required_scopes=required_scopes,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=required_scopes,
                default_scopes=required_scopes,
            ),
            revocation_options=RevocationOptions(enabled=True),
        )

        # In-memory storage
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

        logger.info("ForesightOAuthProvider initialized (base_url=%s)", base_url)

    # -- Client Registration (DCR) -----------------------------------------

    async def register_client(
        self,
        client_info: OAuthClientInformationFull,
    ) -> None:
        """Store a dynamically registered client."""
        self._clients[client_info.client_id] = client_info
        logger.info(
            "Registered OAuth client: id=%s name=%s",
            client_info.client_id,
            client_info.client_name,
        )

    async def get_client(
        self,
        client_id: str,
    ) -> OAuthClientInformationFull | None:
        """Retrieve a registered client by ID."""
        return self._clients.get(client_id)

    # -- Authorization Endpoint (/authorize) -------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Auto-approve the authorization request and return a redirect URL.

        Generates an authorization code (>=160 bits of entropy per RFC 6749
        section 10.10) and appends it to the client's redirect_uri.

        This auto-approves all scopes requested by known clients. If the client
        is unknown, raises ``AuthorizeError``.
        """
        if client.client_id not in self._clients:
            raise AuthorizeError(
                "unauthorized_client",
                "Unknown client_id",
            )

        # Validate redirect_uri
        if not params.redirect_uri:
            raise AuthorizeError(
                "invalid_request",
                "Missing redirect_uri",
            )

        # Generate authorization code (32 bytes = 256 bits)
        code_str = secrets.token_urlsafe(32)
        scopes = params.scopes or []
        now = time.time()

        auth_code = AuthorizationCode(
            code=code_str,
            scopes=scopes,
            expires_at=now + AUTH_CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=None,
        )
        self._auth_codes[code_str] = auth_code

        # Build the redirect URL: {redirect_uri}?code={code}&state={state}
        redirect_base = str(params.redirect_uri)
        query_params: dict[str, str] = {"code": code_str}
        if params.state:
            query_params["state"] = params.state

        # RFC 9207: include issuer parameter
        query_params["iss"] = str(self.issuer_url).rstrip("/")

        separator = "&" if "?" in redirect_base else "?"
        redirect_url = f"{redirect_base}{separator}{urlencode(query_params)}"

        logger.info(
            "Authorization code issued: client=%s code=%s... redirect=%s",
            client.client_id,
            code_str[:8],
            redirect_base,
        )
        return redirect_url

    # -- Token Exchange (/token) ------------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        """Load an authorization code, validating it belongs to the client."""
        code = self._auth_codes.get(authorization_code)
        if code is None:
            return None
        # The code must belong to the requesting client
        if code.client_id != client.client_id:
            return None
        # Check expiry
        if time.time() > code.expires_at:
            self._auth_codes.pop(authorization_code, None)
            return None
        return code

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """Exchange an authorization code for access and refresh tokens.

        Per RFC 6749 section 4.1.2, the authorization code is single-use:
        it is consumed (deleted) immediately after exchange.
        """
        # Validate the code exists and belongs to this client
        stored = self._auth_codes.get(authorization_code.code)
        if stored is None:
            raise TokenError("invalid_grant", "Authorization code not found")
        if stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "Authorization code does not belong to this client")
        if time.time() > stored.expires_at:
            self._auth_codes.pop(authorization_code.code, None)
            raise TokenError("invalid_grant", "Authorization code expired")

        # Consume the code (single-use)
        self._auth_codes.pop(authorization_code.code, None)

        # Issue tokens
        access_token_str = secrets.token_urlsafe(32)
        refresh_token_str = secrets.token_urlsafe(32)
        now = int(time.time())
        scopes = authorization_code.scopes

        access_token = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            resource=authorization_code.resource,
            subject=authorization_code.subject,
            claims={},
        )
        refresh_token = RefreshToken(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
            subject=authorization_code.subject,
        )

        self._access_tokens[access_token_str] = access_token
        self._refresh_tokens[refresh_token_str] = refresh_token

        logger.info(
            "Tokens issued: client=%s access_token=%s... refresh_token=%s...",
            client.client_id,
            access_token_str[:8],
            refresh_token_str[:8],
        )

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh_token_str,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Exchange a refresh token for a new access token (and rotate the refresh token)."""
        stored = self._refresh_tokens.get(refresh_token.token)
        if stored is None:
            raise TokenError("invalid_grant", "Refresh token not found")
        if stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "Refresh token does not belong to this client")
        if stored.expires_at is not None and time.time() > stored.expires_at:
            self._refresh_tokens.pop(refresh_token.token, None)
            raise TokenError("invalid_grant", "Refresh token expired")

        # Rotate: delete old refresh token, issue new pair
        self._refresh_tokens.pop(refresh_token.token, None)

        new_access_str = secrets.token_urlsafe(32)
        new_refresh_str = secrets.token_urlsafe(32)
        now = int(time.time())
        # Use requested scopes or fall back to the token's original scopes
        final_scopes = scopes if scopes else stored.scopes

        new_access = AccessToken(
            token=new_access_str,
            client_id=client.client_id,
            scopes=final_scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            resource=None,
            subject=stored.subject,
            claims={},
        )
        new_refresh = RefreshToken(
            token=new_refresh_str,
            client_id=client.client_id,
            scopes=final_scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
            subject=stored.subject,
        )

        self._access_tokens[new_access_str] = new_access
        self._refresh_tokens[new_refresh_str] = new_refresh

        logger.info(
            "Tokens refreshed: client=%s access_token=%s...",
            client.client_id,
            new_access_str[:8],
        )

        return OAuthToken(
            access_token=new_access_str,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(final_scopes) if final_scopes else None,
            refresh_token=new_refresh_str,
        )

    # -- Token Validation (Bearer token on every MCP call) -----------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Validate a bearer access token. Called on every authenticated MCP request."""
        at = self._access_tokens.get(token)
        if at is None:
            return None
        # Check expiry
        if at.expires_at is not None and time.time() > at.expires_at:
            self._access_tokens.pop(token, None)
            return None
        return at

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        """Load a refresh token, validating it belongs to the client."""
        rt = self._refresh_tokens.get(refresh_token)
        if rt is None:
            return None
        if rt.client_id != client.client_id:
            return None
        if rt.expires_at is not None and time.time() > rt.expires_at:
            self._refresh_tokens.pop(refresh_token, None)
            return None
        return rt

    # -- Revocation --------------------------------------------------------

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke an access or refresh token."""
        token_str = token.token if hasattr(token, "token") else str(token)
        self._access_tokens.pop(token_str, None)
        self._refresh_tokens.pop(token_str, None)
        logger.info("Token revoked: %s...", token_str[:8])
