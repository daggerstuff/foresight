"""Request-scoped tenant context using contextvars.

Replaces the global _tenant_context singleton and TENANT_ID constant
with per-request isolation that works correctly with asyncio and threading.
"""

from __future__ import annotations

from contextvars import ContextVar

from .config import DEFAULT_TENANT_ID

_current_tenant: ContextVar[str] = ContextVar("foresight_tenant_id", default=DEFAULT_TENANT_ID)


def get_current_tenant_id() -> str:
    """Get the tenant ID for the current request context."""
    return _current_tenant.get()


def set_current_tenant_id(tenant_id: str) -> None:
    """Set the tenant ID for the current request context."""
    _current_tenant.set(tenant_id)


def reset_tenant_context() -> None:
    """Reset tenant context to default (for testing)."""
    _current_tenant.set(DEFAULT_TENANT_ID)
