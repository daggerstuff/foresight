"""System capability reporting and diagnostics (FOR-18 / PIX-448).

Reports live capabilities (embedder, reranker, LLM, encryption, Redis)
and detects fallback or degraded execution states across Foresight subsystems.
"""

from __future__ import annotations

import os
from typing import Any

from .encryption import get_encryption_engine
from .reranker import rerank_active, rerank_enabled
from .semantic_search import get_embedder, resolve_provider

try:
    import redis as _redis

    _HAS_REDIS = True
except ImportError:
    _redis = None
    _HAS_REDIS = False


def get_capabilities_report(_tenant_id: str | None = None) -> dict[str, Any]:
    """Inspect all core subsystems and return a structured capability block.

    Returns:
        dict with keys:
          - embedder: provider name, real vs local-hash flag, dimension, model
          - reranker: enabled flag, active flag, provider name
          - llm: configured flag, provider name, model
          - encryption: enabled flag, key_present flag, algorithm
          - redis: configured flag, connected flag
    """
    # 1. Embedder capability
    embedder_provider = resolve_provider()
    try:
        embedder = get_embedder()
        dimension = getattr(embedder, "dimension", 384)
        model = getattr(embedder, "model_name", None)
        actual_provider = getattr(embedder, "provider_name", embedder_provider)
    except Exception:
        dimension = 384
        model = None
        actual_provider = embedder_provider

    embedder_info = {
        "provider": actual_provider,
        "real": actual_provider != "local-hash",
        "dimension": dimension,
        "model": model,
    }

    # 2. Reranker capability
    reranker_info = {
        "enabled": rerank_enabled(),
        "active": rerank_active(),
        "provider": (
            os.environ.get("FORESIGHT_RERANK_PROVIDER", "fastembed").strip() if rerank_enabled() else None
        ),
    }

    # 3. LLM capability
    llm_provider = os.environ.get("FORESIGHT_LLM_PROVIDER", "").strip().lower()
    llm_api_key = os.environ.get("FORESIGHT_LLM_API_KEY", "").strip()
    llm_model = os.environ.get("FORESIGHT_LLM_MODEL", "").strip()
    supported_local = {"ollama", "vllm", "lmstudio", "none"}
    llm_configured = bool(llm_provider and (llm_api_key or llm_provider in supported_local))
    llm_info = {
        "configured": llm_configured,
        "provider": llm_provider or None,
        "model": llm_model or None,
    }

    # 4. Encryption capability
    enc_status = get_encryption_engine().get_status().to_dict()
    enc_info = {
        "enabled": bool(enc_status.get("enabled", False)),
        "key_present": bool(enc_status.get("key_present", False)),
        "algorithm": enc_status.get("algorithm", "AES-256-GCM"),
    }

    # 5. Redis capability
    redis_url = os.environ.get("FORESIGHT_REDIS_URL", "").strip()
    redis_connected = False
    if redis_url and _HAS_REDIS and _redis is not None:
        try:
            r = _redis.from_url(redis_url, socket_connect_timeout=0.5)
            r.ping()
            redis_connected = True
        except Exception:
            redis_connected = False

    redis_info = {
        "configured": bool(redis_url),
        "connected": redis_connected,
    }

    return {
        "embedder": embedder_info,
        "reranker": reranker_info,
        "llm": llm_info,
        "encryption": enc_info,
        "redis": redis_info,
    }
