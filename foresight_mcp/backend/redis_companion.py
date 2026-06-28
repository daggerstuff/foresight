from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_REDIS_IMPORTED = False
try:
    import redis.asyncio as _aioredis
    import redis.exceptions as _redis_exc

    _REDIS_IMPORTED = True
except ImportError:
    _aioredis = None  # type: ignore[assignment]
    _redis_exc = None  # type: ignore[assignment]


_KEY_PREFIX = "foresight"
_EMBED_TTL = 86400  # 24 hours
_RATE_WINDOW = 60  # sliding window in seconds
_INVALIDATE_CHANNEL = "foresight:embedding:invalidate"


def _make_embed_key(tid: str, uid: str, provider: str, text_hash: str) -> str:
    return f"{_KEY_PREFIX}:{tid}:{uid}:embed:{provider}:{text_hash}"


def _make_rate_key(tid: str, uid: str) -> str:
    return f"{_KEY_PREFIX}:{tid}:{uid}:rate"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class RedisCompanion:
    """Optional Redis-backed companion for cross-process caching and rate limiting.

    Provides:
    - Embedding cache with configurable TTL
    - Sliding-window rate limiter per (tenant, user)
    - Pub/sub channel for cache invalidation notifications

    Gracefully degrades when Redis is unavailable — all operations become no-ops
    and the error is logged once.
    """

    def __init__(self, url: str, db: int = 0) -> None:
        self._url = url
        self._db = db
        self._redis: Any = None
        self._available = False
        self._warned = False

    async def _ensure_connected(self) -> bool:
        if self._available:
            return True
        if not _REDIS_IMPORTED:
            if not self._warned:
                logger.warning("redis package not installed; RedisCompanion disabled")
                self._warned = True
            return False
        try:
            self._redis = _aioredis.from_url(  # type: ignore[union-attr]  # type: ignore[union-attr]
                self._url,
                db=self._db,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            await self._redis.ping()
            self._available = True
            logger.info("RedisCompanion connected to %s (db=%d)", self._url, self._db)
        except Exception:
            if not self._warned:
                logger.warning("RedisCompanion: cannot connect to %s; caching disabled", self._url)
                self._warned = True
            self._redis = None
        return self._available

    # ------------------------------------------------------------------
    # Embedding cache
    # ------------------------------------------------------------------

    async def embedding_cache_get(self, text: str, tenant_id: str, user_id: str, provider: str) -> list[float] | None:
        """Return cached embedding vector for *text*, or ``None``."""
        if not await self._ensure_connected():
            return None
        key = _make_embed_key(tenant_id, user_id, provider, _hash_text(text))
        try:
            raw = await self._redis.get(key)
            if raw is None:
                return None
            return json.loads(raw)
        except Exception:
            return None

    async def embedding_cache_set(
        self,
        text: str,
        vector: list[float],
        tenant_id: str,
        user_id: str,
        provider: str,
        ttl: int = _EMBED_TTL,
    ) -> None:
        """Store *vector* in cache with *ttl* seconds."""
        if not await self._ensure_connected():
            return
        key = _make_embed_key(tenant_id, user_id, provider, _hash_text(text))
        try:
            await self._redis.setex(key, ttl, json.dumps(vector))
        except Exception:
            pass

    async def embedding_cache_invalidate(self, tenant_id: str, user_id: str, memory_id: str) -> None:
        """Publish an invalidation notification for *memory_id*."""
        if not await self._ensure_connected():
            return
        payload = json.dumps(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "memory_id": memory_id,
                "ts": time.time(),
            }
        )
        try:
            await self._redis.publish(_INVALIDATE_CHANNEL, payload)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Rate limiter (sliding window via sorted set)
    # ------------------------------------------------------------------

    async def rate_limit_acquire(
        self,
        tenant_id: str,
        user_id: str,
        limit: int = 100,
        window: int = _RATE_WINDOW,
    ) -> bool:
        """Acquire a slot in the sliding-window rate limiter.

        Returns ``True`` if the request is within the limit, ``False`` if
        rate-limited.
        """
        if not await self._ensure_connected():
            return True  # Pass through when Redis unavailable
        key = _make_rate_key(tenant_id, user_id)
        now = time.time()
        cutoff = now - window
        try:
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(key, 0, cutoff)
            pipe.zcard(key)
            results = await pipe.execute()
            count: int = results[1]
            if count >= limit:
                return False
            await self._redis.zadd(key, {str(now): now})
            await self._redis.expire(key, window)
            return True
        except Exception:
            return True  # Pass through on Redis failure

    async def rate_limit_remaining(self, tenant_id: str, user_id: str, window: int = _RATE_WINDOW) -> int:
        """Return remaining request count in the current window."""
        if not await self._ensure_connected():
            return 9999
        key = _make_rate_key(tenant_id, user_id)
        try:
            cutoff = time.time() - window
            await self._redis.zremrangebyscore(key, 0, cutoff)
            return max(0, int(await self._redis.zcard(key)))
        except Exception:
            return 9999

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the Redis connection."""
        if self._redis is not None:
            try:
                await self._redis.close()
            except Exception:
                pass
            self._redis = None
            self._available = False
            self._warned = False
