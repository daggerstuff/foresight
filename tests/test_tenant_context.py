"""Tests for request-scoped tenant context (contextvars)."""
import asyncio

from foresight_mcp.tenant_context import (
    DEFAULT_TENANT_ID,
    get_current_tenant_id,
    reset_tenant_context,
    set_current_tenant_id,
)


def test_get_default_tenant():
    reset_tenant_context()
    assert get_current_tenant_id() == DEFAULT_TENANT_ID


def test_set_tenant_id():
    reset_tenant_context()
    set_current_tenant_id("acme-corp")
    assert get_current_tenant_id() == "acme-corp"


def test_reset_restores_default():
    reset_tenant_context()
    set_current_tenant_id("acme-corp")
    reset_tenant_context()
    assert get_current_tenant_id() == DEFAULT_TENANT_ID


def test_contextvar_isolation_between_tasks():
    """Each asyncio task gets its own tenant context."""
    reset_tenant_context()
    results = {}

    async def task_a():
        set_current_tenant_id("tenant-a")
        await asyncio.sleep(0.01)
        results["a"] = get_current_tenant_id()

    async def task_b():
        set_current_tenant_id("tenant-b")
        await asyncio.sleep(0.01)
        results["b"] = get_current_tenant_id()

    async def main():
        await asyncio.gather(
            asyncio.create_task(task_a()),
            asyncio.create_task(task_b()),
        )

    asyncio.run(main())
    assert results["a"] == "tenant-a"
    assert results["b"] == "tenant-b"


def test_sequential_set_overrides():
    reset_tenant_context()
    set_current_tenant_id("first")
    set_current_tenant_id("second")
    assert get_current_tenant_id() == "second"
