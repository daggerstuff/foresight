"""HTTP REST API surface for Foresight.

Phase A of PIX-4704: REST endpoints that wrap the same pydantic-validated
tool handlers the MCP surface exposes. Parity holds by construction — every
route delegates to the module-level tool function that backs the equivalent
MCP tool.

Auth mirrors the MCP stack:

- API key auth required by default (``FORESIGHT_ALLOW_UNAUTHENTICATED``
  disables it, matching ``AuthMiddleware`` / ``_should_require_api_key``).
- Bearer tokens are tried as API keys, then auth sessions (same order as the
  websocket auth callback in server.py).
- Identity headers ``X-User-Id`` / ``X-Tenant-Id`` are resolved with the same
  allowlist sanitization as ``TenantMiddleware``, set as contextvars for the
  request, and validated against the authenticated user's ``tenant_access``
  list (403 on mismatch).
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth import User, _should_require_api_key, get_auth_manager
from .config import DEFAULT_ACCOUNT_ID, DEFAULT_USER_ID
from .tenant_context import (
    reset_tenant_context,
    set_current_account_id,
    set_current_user_id,
)
from .tenant_middleware import _sanitize_id

_MAX_BATCH_ITEMS = 100
_MAX_LIST_LIMIT = 250


# ---------------------------------------------------------------------------
# Request models (also drive the OpenAPI component schemas)
# ---------------------------------------------------------------------------


class StoreMemoryBody(BaseModel):
    """Body for ``POST /memories`` — mirrors the ``store`` action of ``manage_memories``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    content: str = Field(..., description="Memory content")
    category: str | None = Field(default=None, description="Category label")
    scope: str | None = Field(default=None, description="Memory scope (session, arc, trait, fact)")
    retention: str | None = Field(
        default=None,
        description="Retention policy (ephemeral, short_term, long_term, permanent)",
    )
    importance: float | None = Field(default=None, ge=0.0, le=1.0, description="Importance score (0.0 to 1.0)")
    tags: list[str] | None = Field(default=None, description="Tags list")
    is_sensitive: bool | None = Field(default=None, description="Sensitivity override")
    emotional_context: dict[str, Any] | None = Field(default=None, description="Emotional metadata")
    metrics: dict[str, Any] | None = Field(default=None, description="Empathy metrics")
    relation_type: str | None = Field(
        default=None,
        description="Typed relationship to another memory (updates, extends, derives, contradicts, supports, related)",
    )
    related_memory_id: str | None = Field(default=None, description="Target memory ID for relation_type")


class UpdateMemoryBody(BaseModel):
    """Body for ``PATCH /memories/{id}`` — mirrors the ``update`` action."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    content: str | None = Field(default=None, description="Replacement content")
    category: str | None = Field(default=None, description="New category label")
    scope: str | None = Field(default=None, description="New scope")
    retention: str | None = Field(default=None, description="New retention policy")
    tags: list[str] | None = Field(default=None, description="Replacement tags list")
    is_sensitive: bool | None = Field(default=None, description="Sensitivity override")
    relation_type: str | None = Field(default=None, description="Relationship type")
    related_memory_id: str | None = Field(default=None, description="Related memory ID")


class SearchBody(BaseModel):
    """Body for ``POST /search`` — mirrors ``search_memories`` options."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    query_type: str = Field(
        default="keyword",
        description="Query type: id, keyword, list, or semantic",
    )
    query: str | None = Field(default=None, description="Search query string")
    memory_id: str | None = Field(default=None, description="Memory ID for id lookup")
    limit: int | None = Field(default=None, description="Maximum results")
    offset: int | None = Field(default=None, description="Result offset")
    min_importance: float | None = Field(default=None, description="Minimum importance threshold")
    use_hybrid: bool | None = Field(default=None, description="Enable hybrid search signals")
    use_cascade: bool | None = Field(default=None, description="Enable cascade search across related memories")
    cascade_depth: int | None = Field(default=None, description="Cascade depth")
    cascade_limit: int | None = Field(default=None, description="Cascade limit")
    min_score: float | None = Field(default=None, description="Minimum semantic similarity score")
    provider: str | None = Field(default=None, description="Embedder provider name for semantic search")


class InjectBody(BaseModel):
    """Body for ``POST /inject`` — mirrors ``inject_context``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    conversation_text: str = Field(..., description="Current conversation text to analyze")
    max_memories: int = Field(default=5, description="Maximum memories to return")
    min_relevance: float = Field(default=0.01, description="Minimum relevance score threshold")
    include_details: bool = Field(default=False, description="Return structured details instead of text")
    max_chars: int | None = Field(default=None, description="Character budget for the payload")


class CreateDocumentBody(BaseModel):
    """Body for ``POST /documents`` — mirrors ``create_document``."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    title: str = Field(..., description="Human-readable document title")
    content: str = Field(..., description="Raw source text")
    source: str = Field(
        default="note",
        description="Source type: transcript/article/journal/note/email/other",
    )
    metadata: dict[str, Any] | None = Field(default=None, description="Optional JSON-serializable metadata")
    char_budget: int | None = Field(default=None, ge=100, le=8000, description="Soft max chars per chunk")


class BatchStoreBody(BaseModel):
    """Body for ``POST /memories/batch`` — N independent stores."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    items: list[StoreMemoryBody] = Field(
        ...,
        min_length=1,
        max_length=_MAX_BATCH_ITEMS,
        description="Memories to store (1-100 items)",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"message": message}}, status_code=status)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _authenticate(request: Request) -> tuple[User | None, JSONResponse | None]:
    """Authenticate the request the same way the MCP stack does.

    Returns ``(user, error_response)`` — exactly one is set. ``user`` is None
    when auth is disabled (unauthenticated requests proceed with the default
    identity, matching ``AuthMiddleware`` when its gate is off).
    """
    if not _should_require_api_key():
        return None, None

    token = request.headers.get("x-api-key") or _bearer_token(request)
    if not token:
        return None, _error(
            401,
            "Authentication required: provide Authorization: Bearer <token> or X-API-Key header",
        )

    manager = get_auth_manager()
    user = manager.authenticate_api_key(token) or manager.validate_session(token)
    if not user:
        return None, _error(401, "Invalid or expired credentials")
    return user, None


def _resolve_identity(request: Request) -> tuple[str | None, str, JSONResponse | None]:
    """Resolve user_id / tenant_id from headers with TenantMiddleware sanitization."""
    raw_user = request.headers.get("x-user-id")
    raw_tenant = request.headers.get("x-tenant-id")

    user_id = _sanitize_id(raw_user) if raw_user else None
    if raw_user and user_id is None:
        return None, "", _error(400, "Invalid X-User-Id: must be 1-64 alphanumeric/underscore/hyphen characters")

    if raw_tenant:
        tenant_id = _sanitize_id(raw_tenant)
        if tenant_id is None:
            return (
                None,
                "",
                _error(
                    400,
                    "Invalid X-Tenant-Id: must be 1-64 alphanumeric/underscore/hyphen characters",
                ),
            )
    else:
        tenant_id = DEFAULT_ACCOUNT_ID

    return user_id, tenant_id, None


def _authorize_tenant(user: User | None, tenant_id: str) -> JSONResponse | None:
    if user is not None and not get_auth_manager().validate_user_tenant_access(user, tenant_id):
        return _error(403, f"Tenant access denied for tenant '{tenant_id}'")
    return None


async def _parse_json_body(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    try:
        body = json.loads(await request.body())
    except (UnicodeDecodeError, ValueError):
        return None, _error(400, "Request body must be valid JSON")
    if not isinstance(body, dict):
        return None, _error(400, "Request body must be a JSON object")
    return body, None


def _tool_json(result: str, *, success_status: int = 200) -> JSONResponse:
    """Convert a tool-function return value into an HTTP response.

    Tool handlers return JSON strings (``{"ok": true/false, ...}``) or legacy
    plain-text strings. ``ok: false`` maps to 400; "not found" anywhere in an
    error result (including legacy plain text like ``Memory X not found.``)
    maps to 404.
    """
    if not isinstance(result, str):
        return JSONResponse(result, status_code=success_status)

    lowered = result.lower()
    if result.startswith("Error") or "not found" in lowered:
        status = 404 if "not found" in lowered else 400
        return _error(status, result)

    try:
        data: Any = json.loads(result)
    except (TypeError, ValueError):
        return JSONResponse({"ok": True, "result": result}, status_code=success_status)

    if isinstance(data, dict) and data.get("ok") is False:
        error = data.get("error") or {}
        message = error.get("message", "request failed") if isinstance(error, dict) else str(error)
        status = 404 if "not found" in message.lower() else 400
        return _error(status, message)

    return JSONResponse(data, status_code=success_status)


async def _validated_body(request: Request, model: type[BaseModel]) -> tuple[Any, JSONResponse | None]:
    body, err = await _parse_json_body(request)
    if err:
        return None, err
    try:
        return model(**body), None
    except Exception as exc:
        return None, _error(422, f"Invalid request body: {exc}")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _handle_store_memory(request: Request) -> Response:
    user, auth_err = _authenticate(request)
    if auth_err:
        return auth_err
    user_id, tenant_id, ident_err = _resolve_identity(request)
    if ident_err:
        return ident_err
    tenant_err = _authorize_tenant(user, tenant_id)
    if tenant_err:
        return tenant_err

    body, err = await _validated_body(request, StoreMemoryBody)
    if err:
        return err

    from .server import manage_memories

    set_current_user_id(user_id or DEFAULT_USER_ID)
    set_current_account_id(tenant_id)
    try:
        result = await run_in_threadpool(
            manage_memories,
            user_id=user_id,
            action="store",
            content=body.content,
            category=body.category,
            scope=body.scope,
            retention=body.retention,
            importance=body.importance,
            tags=body.tags,
            is_sensitive=body.is_sensitive,
            emotional_context=body.emotional_context,
            metrics=body.metrics,
            relation_type=body.relation_type,
            related_memory_id=body.related_memory_id,
        )
    finally:
        reset_tenant_context()
    return _tool_json(result, success_status=201)


def _store_one(user_id: str | None, tenant_id: str, body: StoreMemoryBody) -> str:
    from .server import manage_memories

    set_current_user_id(user_id or DEFAULT_USER_ID)
    set_current_account_id(tenant_id)
    try:
        return manage_memories(
            user_id=user_id,
            action="store",
            content=body.content,
            category=body.category,
            scope=body.scope,
            retention=body.retention,
            importance=body.importance,
            tags=body.tags,
            is_sensitive=body.is_sensitive,
            emotional_context=body.emotional_context,
            metrics=body.metrics,
            relation_type=body.relation_type,
            related_memory_id=body.related_memory_id,
        )
    finally:
        reset_tenant_context()


async def _handle_batch_store(request: Request) -> Response:
    user, auth_err = _authenticate(request)
    if auth_err:
        return auth_err
    user_id, tenant_id, ident_err = _resolve_identity(request)
    if ident_err:
        return ident_err
    tenant_err = _authorize_tenant(user, tenant_id)
    if tenant_err:
        return tenant_err

    body, err = await _validated_body(request, BatchStoreBody)
    if err:
        return err

    results = []
    for index, item in enumerate(body.items):
        try:
            result = await run_in_threadpool(_store_one, user_id, tenant_id, item)
            parsed: Any = None
            with contextlib.suppress(TypeError, ValueError):
                parsed = json.loads(result)
            if isinstance(parsed, dict) and parsed.get("ok") is False:
                message = (parsed.get("error") or {}).get("message", "store failed")
                results.append({"index": index, "ok": False, "error": message})
            elif isinstance(result, str) and result.startswith("Error"):
                results.append({"index": index, "ok": False, "error": result})
            else:
                memory_id = parsed.get("memory_id") if isinstance(parsed, dict) else None
                if memory_id is None and isinstance(result, str):
                    match = re.search(r"Stored memory\s+(\S+?)\.", result)
                    if match:
                        memory_id = match.group(1)
                results.append({"index": index, "ok": True, "memory_id": memory_id})
        except Exception as exc:
            # Batch isolation: one failing item must not abort the rest.
            results.append({"index": index, "ok": False, "error": str(exc)})

    all_ok = all(r["ok"] for r in results)
    return JSONResponse(
        {"ok": all_ok, "action": "batch_store", "count": len(results), "results": results},
        status_code=200,
    )


async def _handle_list_memories(request: Request) -> Response:
    user, auth_err = _authenticate(request)
    if auth_err:
        return auth_err
    user_id, tenant_id, ident_err = _resolve_identity(request)
    if ident_err:
        return ident_err
    tenant_err = _authorize_tenant(user, tenant_id)
    if tenant_err:
        return tenant_err

    try:
        limit = min(max(int(request.query_params.get("limit", "50")), 1), _MAX_LIST_LIMIT)
        offset = max(int(request.query_params.get("offset", "0")), 0)
    except ValueError:
        return _error(400, "limit and offset must be integers")

    q = request.query_params.get("q", "")
    set_current_user_id(user_id or DEFAULT_USER_ID)
    set_current_account_id(tenant_id)
    try:
        from .server import search_memories

        if q:
            result = await run_in_threadpool(
                search_memories,
                user_id=user_id,
                query_type="keyword",
                query=q,
                limit=limit,
                offset=offset,
            )
        else:
            result = await run_in_threadpool(
                search_memories,
                user_id=user_id,
                query_type="list",
                limit=limit,
                offset=offset,
            )
    finally:
        reset_tenant_context()

    try:
        items = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        items = None
    if isinstance(items, list):
        return JSONResponse({"ok": True, "items": items, "count": len(items)})
    return _tool_json(result)


async def _handle_patch_memory(request: Request) -> Response:
    user, auth_err = _authenticate(request)
    if auth_err:
        return auth_err
    user_id, tenant_id, ident_err = _resolve_identity(request)
    if ident_err:
        return ident_err
    tenant_err = _authorize_tenant(user, tenant_id)
    if tenant_err:
        return tenant_err

    body, err = await _validated_body(request, UpdateMemoryBody)
    if err:
        return err

    from .server import manage_memories

    set_current_user_id(user_id or DEFAULT_USER_ID)
    set_current_account_id(tenant_id)
    try:
        result = await run_in_threadpool(
            manage_memories,
            user_id=user_id,
            action="update",
            memory_id=request.path_params["memory_id"],
            content=body.content,
            category=body.category,
            scope=body.scope,
            retention=body.retention,
            tags=body.tags,
            is_sensitive=body.is_sensitive,
            relation_type=body.relation_type,
            related_memory_id=body.related_memory_id,
        )
    finally:
        reset_tenant_context()
    return _tool_json(result)


async def _handle_delete_memory(request: Request) -> Response:
    user, auth_err = _authenticate(request)
    if auth_err:
        return auth_err
    user_id, tenant_id, ident_err = _resolve_identity(request)
    if ident_err:
        return ident_err
    tenant_err = _authorize_tenant(user, tenant_id)
    if tenant_err:
        return tenant_err

    archive = request.query_params.get("archive", "").lower() in ("1", "true")
    from .server import manage_memories

    set_current_user_id(user_id or DEFAULT_USER_ID)
    set_current_account_id(tenant_id)
    try:
        result = await run_in_threadpool(
            manage_memories,
            user_id=user_id,
            action="archive" if archive else "delete",
            memory_id=request.path_params["memory_id"],
        )
    finally:
        reset_tenant_context()
    return _tool_json(result)


async def _handle_search(request: Request) -> Response:
    user, auth_err = _authenticate(request)
    if auth_err:
        return auth_err
    user_id, tenant_id, ident_err = _resolve_identity(request)
    if ident_err:
        return ident_err
    tenant_err = _authorize_tenant(user, tenant_id)
    if tenant_err:
        return tenant_err

    body, err = await _validated_body(request, SearchBody)
    if err:
        return err

    from .server import SearchOptions, search_memories

    options = SearchOptions(**body.model_dump(exclude_none=True))
    set_current_user_id(user_id or DEFAULT_USER_ID)
    set_current_account_id(tenant_id)
    try:
        result = await run_in_threadpool(search_memories, user_id=user_id, options=options)
    finally:
        reset_tenant_context()
    return _tool_json(result)


async def _handle_inject(request: Request) -> Response:
    user, auth_err = _authenticate(request)
    if auth_err:
        return auth_err
    user_id, tenant_id, ident_err = _resolve_identity(request)
    if ident_err:
        return ident_err
    tenant_err = _authorize_tenant(user, tenant_id)
    if tenant_err:
        return tenant_err

    body, err = await _validated_body(request, InjectBody)
    if err:
        return err

    from .server import inject_context

    set_current_user_id(user_id or DEFAULT_USER_ID)
    set_current_account_id(tenant_id)
    try:
        result = await run_in_threadpool(
            inject_context,
            user_id=user_id,
            conversation_text=body.conversation_text,
            max_memories=body.max_memories,
            min_relevance=body.min_relevance,
            include_details=body.include_details,
            max_chars=body.max_chars,
        )
    finally:
        reset_tenant_context()

    try:
        data = json.loads(result) if isinstance(result, str) else result
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        return JSONResponse(data)
    return JSONResponse({"formatted": str(result)})


async def _handle_create_document(request: Request) -> Response:
    user, auth_err = _authenticate(request)
    if auth_err:
        return auth_err
    user_id, tenant_id, ident_err = _resolve_identity(request)
    if ident_err:
        return ident_err
    tenant_err = _authorize_tenant(user, tenant_id)
    if tenant_err:
        return tenant_err

    body, err = await _validated_body(request, CreateDocumentBody)
    if err:
        return err

    from .server import create_document

    create_kwargs: dict[str, Any] = {
        "title": body.title,
        "content": body.content,
        "user_id": user_id,
        "source": body.source,
        "metadata": body.metadata,
    }
    if body.char_budget is not None:
        create_kwargs["char_budget"] = body.char_budget

    set_current_user_id(user_id or DEFAULT_USER_ID)
    set_current_account_id(tenant_id)
    try:
        result = await run_in_threadpool(create_document, **create_kwargs)
    finally:
        reset_tenant_context()
    return _tool_json(result, success_status=201)


async def _handle_get_document(request: Request) -> Response:
    user, auth_err = _authenticate(request)
    if auth_err:
        return auth_err
    user_id, tenant_id, ident_err = _resolve_identity(request)
    if ident_err:
        return ident_err
    tenant_err = _authorize_tenant(user, tenant_id)
    if tenant_err:
        return tenant_err

    from .server import get_document

    set_current_user_id(user_id or DEFAULT_USER_ID)
    set_current_account_id(tenant_id)
    try:
        result = await run_in_threadpool(
            get_document,
            document_id=request.path_params["document_id"],
            user_id=user_id,
        )
    finally:
        reset_tenant_context()
    return _tool_json(result)


async def _handle_profile(request: Request) -> Response:
    user, auth_err = _authenticate(request)
    if auth_err:
        return auth_err
    user_id, tenant_id, ident_err = _resolve_identity(request)
    if ident_err:
        return ident_err
    tenant_err = _authorize_tenant(user, tenant_id)
    if tenant_err:
        return tenant_err

    try:
        max_static = int(request.query_params.get("max_static_memories", "20"))
        max_dynamic = int(request.query_params.get("max_dynamic_memories", "10"))
        format_prompt = request.query_params.get("format_prompt", "").lower() in ("1", "true")
    except ValueError:
        return _error(400, "max_static_memories and max_dynamic_memories must be integers")

    from .server import synthesize_profile

    set_current_user_id(user_id or DEFAULT_USER_ID)
    set_current_account_id(tenant_id)
    try:
        result = await run_in_threadpool(
            synthesize_profile,
            user_id=user_id,
            max_static_memories=max_static,
            max_dynamic_memories=max_dynamic,
            format_prompt=format_prompt,
        )
    finally:
        reset_tenant_context()
    return _tool_json(result)


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def _openapi_spec() -> dict[str, Any]:
    """OpenAPI 3.1 spec generated from the REST request models."""
    schemas: dict[str, Any] = {}
    for model in (
        StoreMemoryBody,
        UpdateMemoryBody,
        SearchBody,
        InjectBody,
        CreateDocumentBody,
        BatchStoreBody,
    ):
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        defs = schema.pop("$defs", {})
        schemas[model.__name__] = schema
        schemas.update(defs)

    def _ref(name: str) -> dict[str, Any]:
        return {"$ref": f"#/components/schemas/{name}"}

    def _body(name: str) -> dict[str, Any]:
        return {
            "required": True,
            "content": {"application/json": {"schema": _ref(name)}},
        }

    def _json_response(description: str) -> dict[str, Any]:
        return {
            "description": description,
            "content": {"application/json": {}},
        }

    def _ok() -> dict[str, Any]:
        return {"200": _json_response("Success")}

    list_params = [
        {
            "name": "q",
            "in": "query",
            "required": False,
            "schema": {"type": "string"},
            "description": "Keyword query; omit for a list scan",
        },
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1, "maximum": _MAX_LIST_LIMIT},
        },
        {"name": "offset", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 0}},
    ]
    profile_params = [
        {"name": "max_static_memories", "in": "query", "required": False, "schema": {"type": "integer"}},
        {"name": "max_dynamic_memories", "in": "query", "required": False, "schema": {"type": "integer"}},
        {"name": "format_prompt", "in": "query", "required": False, "schema": {"type": "boolean"}},
    ]

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Foresight REST API",
            "version": "0.1.0",
            "description": (
                "HTTP REST surface over the Foresight memory server. Each endpoint "
                "wraps the same handler as the equivalent MCP tool, with identical "
                "tenant/user scoping and auth enforcement. Auth: Bearer API key or "
                "session token (Authorization header or X-API-Key). Identity: "
                "X-User-Id / X-Tenant-Id headers."
            ),
        },
        "security": [{"bearerAuth": []}],
        "paths": {
            "/memories": {
                "post": {
                    "summary": "Store a memory",
                    "operationId": "storeMemory",
                    "requestBody": _body("StoreMemoryBody"),
                    "responses": {"201": _json_response("Stored"), **_ok()},
                },
                "get": {
                    "summary": "List or keyword-search memories",
                    "operationId": "listMemories",
                    "parameters": list_params,
                    "responses": _ok(),
                },
            },
            "/memories/batch": {
                "post": {
                    "summary": "Store up to 100 memories",
                    "operationId": "batchStoreMemories",
                    "requestBody": _body("BatchStoreBody"),
                    "responses": _ok(),
                }
            },
            "/memories/{memory_id}": {
                "patch": {
                    "summary": "Update a memory",
                    "operationId": "updateMemory",
                    "parameters": [{"name": "memory_id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "requestBody": _body("UpdateMemoryBody"),
                    "responses": _ok(),
                },
                "delete": {
                    "summary": "Delete (or archive) a memory",
                    "operationId": "deleteMemory",
                    "parameters": [
                        {"name": "memory_id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {
                            "name": "archive",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "boolean"},
                            "description": "Soft-archive instead of delete",
                        },
                    ],
                    "responses": _ok(),
                },
            },
            "/search": {
                "post": {
                    "summary": "Search memories",
                    "operationId": "searchMemories",
                    "requestBody": _body("SearchBody"),
                    "responses": _ok(),
                }
            },
            "/inject": {
                "post": {
                    "summary": "Inject context for a conversation",
                    "operationId": "injectContext",
                    "requestBody": _body("InjectBody"),
                    "responses": _ok(),
                }
            },
            "/documents": {
                "post": {
                    "summary": "Ingest a text document and chunk it",
                    "operationId": "createDocument",
                    "requestBody": _body("CreateDocumentBody"),
                    "responses": {"201": _json_response("Created"), **_ok()},
                }
            },
            "/documents/{document_id}": {
                "get": {
                    "summary": "Fetch a stored document",
                    "operationId": "getDocument",
                    "parameters": [
                        {"name": "document_id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": _ok(),
                }
            },
            "/profile": {
                "get": {
                    "summary": "Build the user profile (static + dynamic layers)",
                    "operationId": "getProfile",
                    "parameters": profile_params,
                    "responses": _ok(),
                }
            },
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"},
            },
            "schemas": schemas,
        },
    }


async def _handle_openapi(_request: Request) -> Response:
    return JSONResponse(_openapi_spec())


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

# Registration order matters: /memories/batch must precede /memories/{memory_id}.
_ROUTES: tuple[tuple[str, list[str], str, Any], ...] = (
    ("/openapi.json", ["GET"], "rest_openapi", _handle_openapi),
    ("/memories", ["POST"], "rest_store_memory", _handle_store_memory),
    ("/memories", ["GET"], "rest_list_memories", _handle_list_memories),
    ("/memories/batch", ["POST"], "rest_batch_store", _handle_batch_store),
    ("/memories/{memory_id}", ["PATCH"], "rest_patch_memory", _handle_patch_memory),
    ("/memories/{memory_id}", ["DELETE"], "rest_delete_memory", _handle_delete_memory),
    ("/search", ["POST"], "rest_search", _handle_search),
    ("/inject", ["POST"], "rest_inject", _handle_inject),
    ("/documents", ["POST"], "rest_create_document", _handle_create_document),
    ("/documents/{document_id}", ["GET"], "rest_get_document", _handle_get_document),
    ("/profile", ["GET"], "rest_profile", _handle_profile),
)


def register_rest_routes(mcp: Any) -> None:
    """Mount the REST surface onto the FastMCP server's ASGI app."""
    for path, methods, name, handler in _ROUTES:
        mcp.custom_route(path, methods=methods, name=name, include_in_schema=False)(handler)
