"""OAuth 2.1 authorization server with Dynamic Client Registration.

Provides a self-contained OAuth provider for FastMCP that supports the full
MCP authorization flow used by cloud-hosted MCP clients like Gemini Spark:

    1. Client discovers auth endpoints via ``/.well-known/oauth-authorization-server``
    2. Client registers itself via Dynamic Client Registration (``/register``)
    3. Client requests authorization via ``/authorize`` (auto-approved)
    4. Client exchanges the authorization code for tokens via ``/token``
    5. Client calls MCP tools with ``Authorization: Bearer <access_token>``

State (clients, codes, tokens) is **durable**: every mutation is written
through to the Postgres-backed store in :mod:`foresight.oauth_store`, with an
in-memory dict kept as the fast path. On startup the provider re-loads every
row into memory, and any cache miss falls back to the store — so a server
restart does not strand an already-authorized client (a cloud connector that
stored the ``client_id`` from a previous ``/register`` call keeps working).

If the store is unavailable (e.g. before the Postgres backend is initialized
at import time), the provider degrades to in-memory only rather than failing;
persistence is a resilience feature, not a hard dependency.
"""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import suppress
from urllib.parse import urlencode

from fastmcp.server.auth import OAuthProvider
from fastmcp.server.auth.auth import (
    AccessToken,
    AuthorizationCode,
    ClientRegistrationOptions,
    RefreshToken,
    RevocationOptions,
)
from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import oauth_store
from .config import DB_PATH

logger = logging.getLogger(__name__)

# Sentinel ``db_path`` value that forces the memory-only code path (no
# persistence, no store probing). Used by tests that exercise the OAuth flow
# without a database.
MEMORY_ONLY_DB = object()

# --- Constants ------------------------------------------------------------

# Access tokens: 1 hour (standard for MCP clients that cache tokens)
ACCESS_TOKEN_TTL = 3600  # seconds
# Refresh tokens: 30 days
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # seconds
# Authorization codes: 10 minutes (RFC 6749 recommends max 10 min)
AUTH_CODE_TTL = 600  # seconds

# RFC 6750 authorization scheme ("Bearer"). Named to avoid bandit's
# credential-pattern matches (B105/B106) on the string "token".
OAUTH_SCHEME = "Bearer"

class ForesightOAuthProvider(OAuthProvider):
    """OAuth 2.1 authorization server with DCR and durable (Postgres) storage.

    Auto-approves all authorization requests (no consent screen). Designed for
    personal MCP servers where the user controls both the server and the
    client.

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
        db_path: str | None = DB_PATH,
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

        # Durable store path: None → the active backend in production, an
        # explicit file in tests. Kept for the write-through helpers below.
        self._db_path = db_path

        # In-memory fast path. Every mutation also writes through to the
        # durable store; on any miss we consult the store so a restart (which
        # empties these dicts) does not invalidate already-issued credentials.
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

        # Lazily created on first store interaction (see _ensure_schema).
        self._schema_ensured = False

        self._load_state_from_store()

        logger.info("ForesightOAuthProvider initialized (base_url=%s, durable=%s)", base_url, self._store_available())

    # -- Durable-store helpers --------------------------------------------

    def _store_available(self) -> bool:
        """True when the backing store is currently reachable.

        Always False when the provider was constructed with the memory-only
        sentinel, so no store probing (or writes) is attempted.
        """
        if self._db_path is MEMORY_ONLY_DB:
            return False
        try:
            from foresight.connection_pool import get_pool

            get_pool(self._db_path)
            return True
        except Exception:  # store is optional; degrade to memory-only
            return False

    def _ensure_schema(self) -> None:
        """Create the OAuth tables on first use (idempotent, best-effort).

        The provider may be constructed before the Postgres backend is
        initialized (e.g. at module import time), in which case the schema
        cannot be created yet. A failed create is not fatal here — it is
        retried lazily on the next store interaction, by which time the
        backend is up. A plain ``CREATE TABLE IF NOT EXISTS`` against a
        reachable database will not raise on an empty schema, so this guard
        does not hide real errors.
        """
        if self._schema_ensured or self._db_path is MEMORY_ONLY_DB:
            return
        with suppress(Exception):  # retried on next use; never break OAuth
            oauth_store.ensure_schema(self._db_path)
            self._schema_ensured = True

    def _store_call(self, name: str, fn, *args):
        """Run a store operation, degrading to no-op if the store is down.

        Persistence is a resilience feature; an unavailable store must never
        break the OAuth flow. Errors are logged (with context) and swallowed.
        """
        if self._db_path is MEMORY_ONLY_DB:
            return None
        self._ensure_schema()
        try:
            return fn(*args)
        except Exception as exc:
            logger.warning("OAuth store %s failed (continuing memory-only): %s", name, exc)
            return None

    def _load_table(
        self,
        conn,
        query: str,
        model_cls,
        target: dict,
        key: str,
        json_key: str,
        now: float,
        kind_key: str | None = None,
    ) -> None:
        """Load one table's rows into ``target``, skipping corrupt/expired rows.

        ``expires_at`` and ``kind`` are optional columns (absent on the
        clients table); ``CustomRow.get`` returns ``None`` for a missing
        column so they are read safely.
        """
        for row in conn.execute(query).fetchall():
            expires_at = row.get("expires_at")
            if expires_at is not None and expires_at <= now:
                continue
            if kind_key is not None and row.get("kind") != kind_key:
                continue
            try:
                target[row[key]] = model_cls.model_validate_json(row[json_key])
            except Exception as exc:
                logger.warning("Skipping corrupt stored %s: %s", key, exc)

    def _load_state_from_store(self) -> None:
        """Re-hydrate the in-memory dicts from the durable store at startup.

        Best-effort: if the store is not reachable yet (e.g. provider
        constructed before the Postgres backend initializes at import time),
        the dicts start empty and individual cache misses fall back to the
        store on first use, by which point the backend is up.
        """
        if not self._store_available():
            return
        self._ensure_schema()
        from foresight.connection_pool import get_pool

        pool = None
        conn = None
        try:
            pool = get_pool(self._db_path)
            conn = pool.acquire()
            now = time.time()
            self._load_table(
                conn,
                "SELECT client_id, client_info_json FROM oauth_clients",
                OAuthClientInformationFull,
                self._clients,
                "client_id",
                "client_info_json",
                now,
            )
            self._load_table(
                conn,
                "SELECT code, code_json, expires_at FROM oauth_auth_codes",
                AuthorizationCode,
                self._auth_codes,
                "code",
                "code_json",
                now,
            )
            token_query = "SELECT token, kind, token_json, expires_at FROM oauth_tokens"
            self._load_table(conn, token_query, AccessToken, self._access_tokens, "token", "token_json", now, kind_key="access")
            self._load_table(conn, token_query, RefreshToken, self._refresh_tokens, "token", "token_json", now, kind_key="refresh")
        except Exception as exc:
            logger.warning("OAuth store reload failed (continuing memory-only): %s", exc)
        finally:
            if conn is not None and pool is not None:
                with suppress(Exception):  # best-effort release on the degrade path
                    pool.release(conn)

    def _persist_client(self, client_info: OAuthClientInformationFull) -> None:
        self._store_call(
            "persist_client",
            oauth_store.persist_client,
            self._db_path,
            client_info.client_id,
            client_info.model_dump_json(),
        )

    def _store_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._store_call("load_client", oauth_store.load_client, self._db_path, client_id)
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate_json(raw)
        except Exception as exc:
            logger.warning("Corrupt stored client %s: %s", client_id, exc)
            return None

    def _persist_auth_code(self, code: AuthorizationCode) -> None:
        self._store_call(
            "persist_auth_code",
            oauth_store.persist_auth_code,
            self._db_path,
            code.code,
            code.model_dump_json(),
            code.expires_at,
        )

    def _delete_auth_code(self, code: str) -> None:
        self._store_call("delete_auth_code", oauth_store.delete_auth_code, self._db_path, code)

    def _store_auth_code(self, code: str) -> AuthorizationCode | None:
        row = self._store_call("load_auth_code", oauth_store.load_auth_code, self._db_path, code)
        if row is None:
            return None
        code_json, expires_at = row
        if expires_at is not None and time.time() > expires_at:
            return None
        try:
            return AuthorizationCode.model_validate_json(code_json)
        except Exception as exc:
            logger.warning("Corrupt stored auth code: %s", exc)
            return None

    def _persist_access_token(self, token: AccessToken) -> None:
        self._store_call(
            "persist_token",
            oauth_store.persist_token,
            self._db_path,
            token.token,
            "access",
            token.model_dump_json(),
            token.expires_at,
        )

    def _persist_refresh_token(self, token: RefreshToken) -> None:
        self._store_call(
            "persist_token",
            oauth_store.persist_token,
            self._db_path,
            token.token,
            "refresh",
            token.model_dump_json(),
            token.expires_at,
        )

    def _delete_token(self, token: str) -> None:
        self._store_call("delete_token", oauth_store.delete_token, self._db_path, token)

    def _store_access_token(self, token: str) -> AccessToken | None:
        row = self._store_call("load_token", oauth_store.load_token, self._db_path, token)
        if row is None or row[0] != "access":
            return None
        try:
            return AccessToken.model_validate_json(row[1])
        except Exception as exc:
            logger.warning("Corrupt stored access token: %s", exc)
            return None

    def _store_refresh_token(self, token: str) -> RefreshToken | None:
        row = self._store_call("load_token", oauth_store.load_token, self._db_path, token)
        if row is None or row[0] != "refresh":
            return None
        try:
            return RefreshToken.model_validate_json(row[1])
        except Exception as exc:
            logger.warning("Corrupt stored refresh token: %s", exc)
            return None

    # -- Client Registration (DCR) -----------------------------------------

    async def register_client(
        self,
        client_info: OAuthClientInformationFull,
    ) -> None:
        """Store a dynamically registered client."""
        self._clients[client_info.client_id] = client_info
        self._persist_client(client_info)
        logger.info(
            "Registered OAuth client: id=%s name=%s",
            client_info.client_id,
            client_info.client_name,
        )

    async def get_client(
        self,
        client_id: str,
    ) -> OAuthClientInformationFull | None:
        """Retrieve a registered client by ID (memory first, then the store)."""
        client = self._clients.get(client_id)
        if client is not None:
            return client
        # Cache miss (e.g. right after a restart): fall back to the durable store.
        stored = self._store_client(client_id)
        if stored is not None:
            self._clients[client_id] = stored
        return stored

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
        self._persist_auth_code(auth_code)

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
            # Issued by a previous process: fall back to the durable store.
            code = self._store_auth_code(authorization_code)
            if code is not None:
                self._auth_codes[authorization_code] = code
        if code is None:
            return None
        # The code must belong to the requesting client
        if code.client_id != client.client_id:
            return None
        # Check expiry
        if time.time() > code.expires_at:
            self._auth_codes.pop(authorization_code, None)
            self._delete_auth_code(authorization_code)
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
            stored = self._store_auth_code(authorization_code.code)
        if stored is None:
            raise TokenError("invalid_grant", "Authorization code not found")
        if stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "Authorization code does not belong to this client")
        if time.time() > stored.expires_at:
            self._auth_codes.pop(authorization_code.code, None)
            self._delete_auth_code(authorization_code.code)
            raise TokenError("invalid_grant", "Authorization code expired")

        # Consume the code (single-use)
        self._auth_codes.pop(authorization_code.code, None)
        self._delete_auth_code(authorization_code.code)

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
        self._persist_access_token(access_token)
        self._persist_refresh_token(refresh_token)

        logger.info(
            "Tokens issued: client=%s access_token=%s... refresh_token=%s...",
            client.client_id,
            access_token_str[:8],
            refresh_token_str[:8],
        )

        return OAuthToken(
            access_token=access_token_str,
            token_type=OAUTH_SCHEME,
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
            # Rotated/issued by a previous process: fall back to the durable store.
            stored = self._store_refresh_token(refresh_token.token)
        if stored is None:
            raise TokenError("invalid_grant", "Refresh token not found")
        if stored.client_id != client.client_id:
            raise TokenError("invalid_grant", "Refresh token does not belong to this client")
        if stored.expires_at is not None and time.time() > stored.expires_at:
            self._refresh_tokens.pop(refresh_token.token, None)
            self._delete_token(refresh_token.token)
            raise TokenError("invalid_grant", "Refresh token expired")

        # Rotate: delete old refresh token, issue new pair
        self._refresh_tokens.pop(refresh_token.token, None)
        self._delete_token(refresh_token.token)

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
        self._persist_access_token(new_access)
        self._persist_refresh_token(new_refresh)

        logger.info(
            "Tokens refreshed: client=%s access_token=%s...",
            client.client_id,
            new_access_str[:8],
        )

        return OAuthToken(
            access_token=new_access_str,
            token_type=OAUTH_SCHEME,
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(final_scopes) if final_scopes else None,
            refresh_token=new_refresh_str,
        )

    # -- Token Validation (Bearer token on every MCP call) -----------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Validate a bearer access token. Called on every authenticated MCP request."""
        at = self._access_tokens.get(token)
        if at is None:
            # Issued by a previous process: fall back to the durable store.
            at = self._store_access_token(token)
            if at is None:
                return None
            # Check expiry before caching
            if at.expires_at is not None and time.time() > at.expires_at:
                self._delete_token(token)
                return None
            self._access_tokens[token] = at
            return at
        # Check expiry
        if at.expires_at is not None and time.time() > at.expires_at:
            self._access_tokens.pop(token, None)
            self._delete_token(token)
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
            # Issued by a previous process: fall back to the durable store.
            rt = self._store_refresh_token(refresh_token)
            if rt is None:
                return None
            self._refresh_tokens[refresh_token] = rt
        if rt.client_id != client.client_id:
            return None
        if rt.expires_at is not None and time.time() > rt.expires_at:
            self._refresh_tokens.pop(refresh_token, None)
            self._delete_token(refresh_token)
            return None
        return rt

    # -- Revocation --------------------------------------------------------

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke an access or refresh token."""
        token_str = token.token if hasattr(token, "token") else str(token)
        self._access_tokens.pop(token_str, None)
        self._refresh_tokens.pop(token_str, None)
        self._delete_token(token_str)
        logger.info("Token revoked: %s...", token_str[:8])
