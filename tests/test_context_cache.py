"""Unit tests for Foresight in-memory session context cache."""

from __future__ import annotations

import time

from foresight.context_cache import ContextCache


def test_context_cache_set_and_get():
    cache = ContextCache(max_entries=10, default_ttl_seconds=5.0)
    key = cache.compute_key(user_id="u1", tenant_id="t1", query="test query")

    assert cache.get(key) is None

    cache.set(key, "cached context value", user_id="u1")
    assert cache.get(key) == "cached context value"


def test_context_cache_invalidation_by_user():
    cache = ContextCache()
    key1 = cache.compute_key(user_id="u1", tenant_id="t1", query="query 1")
    key2 = cache.compute_key(user_id="u1", tenant_id="t1", query="query 2")
    key3 = cache.compute_key(user_id="u2", tenant_id="t1", query="query 3")

    u1_keys = (key1, key2)
    cache.set(key1, "val1", user_id="u1")
    cache.set(key2, "val2", user_id="u1")
    cache.set(key3, "val3", user_id="u2")

    assert cache.get(key1) == "val1"
    assert cache.get(key2) == "val2"
    assert cache.get(key3) == "val3"

    # Invalidate only u1
    invalidated = cache.invalidate_user("u1")
    assert invalidated == len(u1_keys)
    assert cache.get(key1) is None
    assert cache.get(key2) is None
    assert cache.get(key3) == "val3"


def test_context_cache_ttl_expiration():
    cache = ContextCache(default_ttl_seconds=0.1)
    key = cache.compute_key(user_id="u1", tenant_id="t1", query="expiring query")

    cache.set(key, "fresh val", user_id="u1", ttl_seconds=0.1)
    assert cache.get(key) == "fresh val"

    time.sleep(0.15)
    assert cache.get(key) is None


def test_context_cache_stats():
    cache = ContextCache()
    key = cache.compute_key(user_id="u1", tenant_id="t1", query="stat query")

    cache.get(key)  # Miss
    cache.set(key, "data", user_id="u1")
    cache.get(key)  # Hit
    cache.get(key)  # Hit

    stats = cache.get_stats()
    total_lookups = 3
    total_hits = 2
    assert stats["total_lookups"] == total_lookups
    assert stats["total_hits"] == total_hits
    assert stats["total_misses"] == total_lookups - total_hits
    assert stats["hit_rate_pct"] == round((total_hits / total_lookups) * 100, 2)
