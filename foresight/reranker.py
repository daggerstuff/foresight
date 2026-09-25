"""Cross-encoder reranking stage for hybrid retrieval (PIX-4701).

Optional final stage that re-scores the fused candidate set against the
query with a local cross-encoder (fastembed's rerank API, default
``BAAI/bge-reranker-base``). Like the embedder providers, reranking is
local-only: model weights are fetched once by fastembed and inference
runs in-process, so enabling it does not break the no-egress guarantee.

Off by default. ``FORESIGHT_RERANK_ENABLED`` turns the stage on;
``FORESIGHT_RERANK_PROVIDER`` selects the implementation (currently
``fastembed``). When the flag is off — or on but the dependency is
missing — the stage is a strict no-op: retrieval results and ordering
are identical to an un-reranked run.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import os
import threading
from typing import Protocol

logger = logging.getLogger("foresight_reranker")

DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-base"
DEFAULT_RERANKER_PROVIDER = "fastembed"
VALID_RERANK_PROVIDERS: frozenset[str] = frozenset({DEFAULT_RERANKER_PROVIDER})

# A raw cross-encoder score is unbounded; folding it directly into the
# fused ranking score would let one signal dominate. Bounded multiplier
# band instead: sigmoid(score) in (0, 1) shifted into (0.5, 1.5), so a
# highly relevant document gets up to a 1.5x boost and an irrelevant one
# at least a 0.5x penalty — enough to reorder, never to erase.
MULTIPLIER_FLOOR = 0.5


class Reranker(Protocol):
    """Protocol for reranking providers."""

    provider_name: str
    model: str

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Return one relevance score per document, in document order."""
        ...


def rerank_enabled() -> bool:
    """True when ``FORESIGHT_RERANK_ENABLED`` opts the stage in.

    Read at call time so tests and deployments can toggle it without a
    process restart. Off by default.
    """
    return os.environ.get("FORESIGHT_RERANK_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _fastembed_rerank_available() -> bool:
    return importlib.util.find_spec("fastembed") is not None


class FastEmbedReranker:
    """Local ONNX cross-encoder reranker via the optional fastembed package."""

    provider_name = DEFAULT_RERANKER_PROVIDER

    def __init__(self, model: str = DEFAULT_RERANKER_MODEL) -> None:
        from fastembed.rerank.cross_encoder import CrossEncoder

        self.model = model
        self._encoder = CrossEncoder(model_name=model)

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        items = list(self._encoder.rerank(query, documents))
        if items and isinstance(items[0], dict):
            # fastembed yields {"index": int, "score": float}; restore
            # document order so callers can zip scores with documents.
            ordered = sorted(items, key=lambda item: item["index"])
            return [float(item["score"]) for item in ordered]
        return [float(item) for item in items]


_RERANKER_CACHE: dict[str, Reranker] = {}
_RERANKER_CACHE_LOCK = threading.Lock()


def get_reranker(provider: str | None = None) -> Reranker | None:
    """Return a cached reranker, or ``None`` when disabled/unavailable.

    Never raises: an enabled-but-unavailable reranker degrades to a
    logged no-op so retrieval keeps working unchanged.
    """
    if not rerank_enabled():
        return None
    name = (provider or os.environ.get("FORESIGHT_RERANK_PROVIDER", "") or DEFAULT_RERANKER_PROVIDER).strip().lower()
    if name not in VALID_RERANK_PROVIDERS:
        logger.warning(
            "unknown rerank provider %r; valid: %s — reranking disabled", name, sorted(VALID_RERANK_PROVIDERS)
        )
        return None
    if not _fastembed_rerank_available():
        logger.warning(
            "FORESIGHT_RERANK_ENABLED set but the optional %s dependency is not installed — reranking disabled", name
        )
        return None
    with _RERANKER_CACHE_LOCK:
        cached = _RERANKER_CACHE.get(name)
        if cached is None:
            try:
                cached = FastEmbedReranker()
            except Exception as exc:  # model download/load failures must not break search
                logger.warning("reranker init failed for %s: %s — reranking disabled", name, exc)
                return None
            _RERANKER_CACHE[name] = cached
        return cached


def rerank_active() -> bool:
    """True only when the stage is enabled AND a reranker is available."""
    return rerank_enabled() and get_reranker() is not None


def score_to_multiplier(score: float) -> float:
    """Map a raw relevance score to a bounded post-fusion multiplier in (0.5, 1.5)."""
    return MULTIPLIER_FLOOR + _sigmoid(score)


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    exp_x = math.exp(x)
    return exp_x / (1.0 + exp_x)
