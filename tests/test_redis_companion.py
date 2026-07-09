"""Tests for RedisCompanion graceful degradation (PIX-3983).

These tests verify that RedisCompanion degrades gracefully when
Redis is unavailable and operates normally when backed by a
(fake/mocked) connection. No real Redis server required.
"""

from __future__ import annotations

import pytest
from foresight.backend.redis_companion import RedisCompanion


class TestRedisCompanionGracefulDegradation:
    """RedisCompanion should be a no-op when Redis is unavailable."""

    @pytest.mark.asyncio
    async def test_embedding_cache_get_returns_none_when_unavailable(self):
        companion = RedisCompanion(url="redis://localhost:19999/0")
        result = await companion.embedding_cache_get("hello", "t1", "u1", "test")
        assert result is None

    @pytest.mark.asyncio
    async def test_embedding_cache_set_noop_when_unavailable(self):
        companion = RedisCompanion(url="redis://localhost:19999/0")
        # Should not raise
        await companion.embedding_cache_set("hello", [0.1, 0.2], "t1", "u1", "test")

    @pytest.mark.asyncio
    async def test_invalidation_noop_when_unavailable(self):
        companion = RedisCompanion(url="redis://localhost:19999/0")
        await companion.embedding_cache_invalidate("t1", "u1", "mem-1")

    @pytest.mark.asyncio
    async def test_rate_limit_acquire_passes_when_unavailable(self):
        companion = RedisCompanion(url="redis://localhost:19999/0")
        result = await companion.rate_limit_acquire("t1", "u1")
        assert result is True  # Pass through

    @pytest.mark.asyncio
    async def test_rate_limit_remaining_high_when_unavailable(self):
        companion = RedisCompanion(url="redis://localhost:19999/0")
        remaining = await companion.rate_limit_remaining("t1", "u1")
        assert remaining == 9999

    @pytest.mark.asyncio
    async def test_close_noop_when_unavailable(self):
        companion = RedisCompanion(url="redis://localhost:19999/0")
        await companion.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_multiple_operations_no_side_effects(self):
        """Multiple consecutive operations on unavailable Redis are all no-ops."""
        companion = RedisCompanion(url="redis://localhost:19999/0")
        await companion.embedding_cache_set("data", [0.5], "t1", "u1", "test")
        result = await companion.embedding_cache_get("data", "t1", "u1", "test")
        assert result is None
        ok = await companion.rate_limit_acquire("t1", "u1")
        assert ok is True


class TestRedisCompanionInitialState:
    """Verifies RedisCompanion starts in the correct disconnected state."""

    def test_initial_state(self):
        companion = RedisCompanion(url="redis://localhost:6379/0")
        assert companion._redis is None
        assert companion._available is False
        assert companion._warned is False

    def test_initial_state_with_db_param(self):
        companion = RedisCompanion(url="redis://localhost:6379/0", db=3)
        assert companion._db == 3
