"""FastMCP middleware that resolves tenant from request context."""
from __future__ import annotations

import logging

from fastmcp.server.middleware import Middleware as _Middleware

from .config import DEFAULT_TENANT_ID
from .tenant_context import get_current_tenant_id, set_current_tenant_id

logger = logging.getLogger(__name__)


class TenantMiddleware(_Middleware):
    """Resolves tenant_id from request context and sets the contextvar.

    Resolution order:
    1. Tool argument ``tenant_id`` (if provided)
    2. Request metadata ``_meta`` (if available from MCP transport)
    3. DEFAULT_TENANT_ID

    After resolution, the tenant ID is stored in the contextvar so that
    downstream code (SQL queries, graph store, etc.) can access it via
    ``get_current_tenant_id()`` without threading the parameter through
    every function call.
    """

    async def on_call_tool(self, context, call_next):
        tenant_id = self._resolve_tenant(context)
        set_current_tenant_id(tenant_id)
        try:
            return await call_next(context)
        finally:
            set_current_tenant_id(DEFAULT_TENANT_ID)

    def _resolve_tenant(self, context) -> str:
        # Try tool arguments first
        message = getattr(context, 'message', None)
        if message:
            arguments = getattr(message, 'arguments', None) or {}
            if isinstance(arguments, dict) and "tenant_id" in arguments:
                return arguments["tenant_id"]

        # Try request metadata
        if message:
            meta = getattr(message, 'meta', None)
            if meta and hasattr(meta, 'model_extra') and meta.model_extra:
                tid = meta.model_extra.get("tenant_id")
                if tid:
                    return tid

        return DEFAULT_TENANT_ID
