"""System capability reporting and diagnostics (FOR-18 / PIX-448).

Reports live capabilities (embedder, reranker, LLM, encryption, Redis)
and detects fallback or degraded execution states across Foresight subsystems.
"""

from __future__ import annotations

import os
from typing import Any

from .encryption import get_encryption_engine
from .llm_client import LLMConfig
from .reranker import rerank_active, rerank_enabled
from .semantic_search import get_embedder, resolve_provider
from .tenant_context import get_current_account_id

try:
    import redis as _redis_mod

    _redis: Any = _redis_mod
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
    # 1. Embedder capability. Provider resolution and initialization both fail
    # for a bad provider name or a broken optional dependency; report that as
    # unavailable instead of crashing the whole diagnostic (or, worse, looking
    # healthy with a made-up dimension).
    embedder_error: str | None = None
    try:
        embedder_provider = resolve_provider()
        embedder = get_embedder()
        dimension = getattr(embedder, "dimension", 384)
        model = getattr(embedder, "model_name", None)
        actual_provider = getattr(embedder, "provider_name", embedder_provider)
    except Exception as exc:
        embedder_error = str(exc)
        dimension = None
        model = None
        actual_provider = os.environ.get("FORESIGHT_EMBEDDING_PROVIDER", "").strip() or "unknown"

    embedder_info: dict[str, Any] = {
        "provider": actual_provider,
        "real": embedder_error is None and actual_provider != "local-hash",
        "available": embedder_error is None,
        "dimension": dimension,
        "model": model,
    }
    if embedder_error:
        embedder_info["error"] = embedder_error

    # 2. Reranker capability
    reranker_info = {
        "enabled": rerank_enabled(),
        "active": rerank_active(),
        "provider": (os.environ.get("FORESIGHT_RERANK_PROVIDER", "fastembed").strip() if rerank_enabled() else None),
    }

    # 3. LLM capability. Use the same resolution the client uses (default
    # provider, per-tenant key, provider-specific env fallbacks) so this
    # report agrees with what synthesis will actually do.
    supported_local = {"ollama", "vllm", "lmstudio"}
    try:
        llm_cfg = LLMConfig.from_env(_tenant_id or get_current_account_id())
    except Exception as exc:
        llm_info: dict[str, Any] = {
            "configured": False,
            "provider": os.environ.get("FORESIGHT_LLM_PROVIDER", "").strip() or None,
            "model": None,
            "error": str(exc),
        }
    else:
        llm_info = {
            "configured": llm_cfg.provider not in ("", "none")
            and (bool(llm_cfg.api_key) or llm_cfg.provider in supported_local),
            "provider": llm_cfg.provider or None,
            "model": llm_cfg.model_for_provider() or None,
        }

    # 4. Encryption capability
    enc_status = get_encryption_engine().get_status().to_dict()
    enc_info = {
        "enabled": bool(enc_status.get("enabled", False)),
        "key_present": bool(enc_status.get("key_configured", False)),
        "algorithm": enc_status.get("algorithm", "AES-256-GCM"),
    }

    # 5. Redis capability
    redis_url = (os.environ.get("FORESIGHT_REDIS_URL") or os.environ.get("REDIS_URL") or "").strip()
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
