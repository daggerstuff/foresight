"""RRF Weight Tuning and Configuration.

Provides configurable RRF (Reciprocal Rank Fusion) weights and
grid search utilities for optimizing retrieval performance.

Current default weights (subject to tuning):
- keyword: 1.0 (primary relevance signal)
- tfidf_cosine: 0.7 (topical similarity via TF-IDF)
- graph: 0.8 (entity expansion value)
- temporal: 0.6 (recency context)
- RRF k: 60 (standard smoothing constant)
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import math as _math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("foresight_rrf_tuning")


DEFAULT_GATING_THRESHOLD: float = 0.015
# Finite "gate everything" sentinel: RRFConfig.from_dict and the hook's
# persisted-config loader both reject infinity, so calibration uses a value
# no real fused score can reach instead.
GATE_ALL_THRESHOLD: float = 1e9


@dataclass
class RRFConfig:
    """Configuration for RRF fusion weights.

    In addition to per-signal weights, supports trend modifiers and
    category half-life multipliers for the temporal signal, plus
    optional entity_weight that can be injected into RRF as a
    separate ranking pass.
    """

    rrf_k: float = 60.0  # Smoothing constant
    keyword_weight: float = 1.0
    tfidf_cosine_weight: float = 0.7
    graph_weight: float = 0.8
    temporal_weight: float = 0.8  # Aligned with HybridRetriever.DEFAULT_WEIGHTS
    entity_weight: float = 0.5  # Separate entity-salience pass (0 = disabled)
    gating_threshold: float = DEFAULT_GATING_THRESHOLD  # Min fused score for recall injection

    # Trend modifiers for temporal scoring
    trend_mod_strengthening: float = 1.2
    trend_mod_stable: float = 1.0
    trend_mod_weakening: float = 0.8
    trend_mod_stale: float = 0.5

    # Category half-life multipliers
    category_mult_session: float = 0.5
    category_mult_fact: float = 1.0
    category_mult_preference: float = 1.5
    category_mult_trait: float = 2.0

    def to_dict(self) -> dict[str, float]:
        """Convert to dictionary for storage."""
        d: dict[str, float] = {
            "rrf_k": self.rrf_k,
            "keyword": self.keyword_weight,
            "tfidf_cosine": self.tfidf_cosine_weight,
            "graph": self.graph_weight,
            "temporal": self.temporal_weight,
            "entity": self.entity_weight,
            "gating_threshold": self.gating_threshold,
        }
        for k in ("strengthening", "stable", "weakening", "stale"):
            d[f"trend_mod_{k}"] = getattr(self, f"trend_mod_{k}")
        for cat in ("session", "fact", "preference", "trait"):
            d[f"category_mult_{cat}"] = getattr(self, f"category_mult_{cat}")
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RRFConfig:
        """Create from dictionary, coercing all values to finite positive floats."""

        def _num(val: Any, default: float) -> float:
            try:
                f = float(val)
            except (TypeError, ValueError):
                return default
            return f if (_math.isfinite(f) and f >= 0) else default

        return cls(
            rrf_k=_num(data.get("rrf_k", 60.0), 60.0),
            keyword_weight=_num(data.get("keyword", 1.0), 1.0),
            tfidf_cosine_weight=_num(data.get("tfidf_cosine", 0.7), 0.7),
            graph_weight=_num(data.get("graph", 0.8), 0.8),
            temporal_weight=_num(data.get("temporal", 0.8), 0.8),
            entity_weight=_num(data.get("entity", 0.0), 0.0),
            gating_threshold=_num(data.get("gating_threshold", DEFAULT_GATING_THRESHOLD), DEFAULT_GATING_THRESHOLD),
            trend_mod_strengthening=_num(data.get("trend_mod_strengthening", 1.2), 1.2),
            trend_mod_stable=_num(data.get("trend_mod_stable", 1.0), 1.0),
            trend_mod_weakening=_num(data.get("trend_mod_weakening", 0.8), 0.8),
            trend_mod_stale=_num(data.get("trend_mod_stale", 0.5), 0.5),
            category_mult_session=_num(data.get("category_mult_session", 0.5), 0.5),
            category_mult_fact=_num(data.get("category_mult_fact", 1.0), 1.0),
            category_mult_preference=_num(data.get("category_mult_preference", 1.5), 1.5),
            category_mult_trait=_num(data.get("category_mult_trait", 2.0), 2.0),
        )

    def to_json_file(self, path: str) -> None:
        """Save configuration to JSON file."""
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def from_json_file(cls, path: str) -> RRFConfig:
        """Load configuration from JSON file."""
        with open(path) as f:
            data = json.load(f)
        return cls.from_dict(data)


# Default configuration file location
DEFAULT_CONFIG_PATH = Path.home() / ".foresight" / "rrf_config.json"


def get_rrf_config(config_path: str | None = None) -> RRFConfig:
    """
    Get RRF configuration from file or return defaults.

    Args:
        config_path: Path to config file (default: ~/.foresight/rrf_config.json)

    Returns:
        RRFConfig with loaded or default values
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH

    if path.exists():
        try:
            config = RRFConfig.from_json_file(str(path))
            logger.info(f"Loaded RRF config from {path}")
            return config
        except Exception as e:
            logger.warning(f"Failed to load RRF config: {e}. Using defaults.")

    return RRFConfig()


def save_rrf_config(config: RRFConfig, config_path: str | None = None) -> None:
    """
    Save RRF configuration to file.

    Args:
        config: Configuration to save
        config_path: Path to config file (default: ~/.foresight/rrf_config.json)
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    config.to_json_file(str(path))
    logger.info(f"Saved RRF config to {path}")


@dataclass
class GridSearchResult:
    """Result from grid search optimization."""

    best_weights: dict[str, float]
    best_score: float
    all_results: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "best_weights": self.best_weights,
            "best_score": self.best_score,
            "num_evaluated": len(self.all_results),
        }


@dataclass(frozen=True)
class WeightRanges:
    """Search ranges for one grid search, each as a ``(min, max, step)`` triple."""

    keyword: tuple[float, float, float] = (0.5, 2.0, 0.5)
    tfidf: tuple[float, float, float] = (0.3, 1.0, 0.2)
    graph: tuple[float, float, float] = (0.4, 1.2, 0.2)
    temporal: tuple[float, float, float] = (0.2, 1.0, 0.2)
    entity: tuple[float, float, float] | None = None


def grid_search_weights(
    ranges: WeightRanges | None = None,
    *,
    evaluate_fn: Callable[[dict[str, float]], float] | None = None,
) -> GridSearchResult:
    """
    Grid search for optimal RRF weights.

    Args:
        ranges: ``(min, max, step)`` search ranges per weight; see
            :class:`WeightRanges`.
        evaluate_fn: Function that takes weights dict and returns score

    Returns:
        GridSearchResult with best weights and all results

    Note:
        evaluate_fn should be provided for actual tuning.
    Without it, returns placeholder results.
    """
    ranges = ranges or WeightRanges()
    keyword_values = list(frange(ranges.keyword[0], ranges.keyword[1] + 0.001, ranges.keyword[2]))
    tfidf_values = list(frange(ranges.tfidf[0], ranges.tfidf[1] + 0.001, ranges.tfidf[2]))
    graph_values = list(frange(ranges.graph[0], ranges.graph[1] + 0.001, ranges.graph[2]))
    temporal_values = list(frange(ranges.temporal[0], ranges.temporal[1] + 0.001, ranges.temporal[2]))
    entity_values = (
        list(frange(ranges.entity[0], ranges.entity[1] + 0.001, ranges.entity[2])) if ranges.entity else [0.0]
    )

    all_combinations = list(
        itertools.product(keyword_values, tfidf_values, graph_values, temporal_values, entity_values)
    )

    best_score = float("-inf")
    best_weights: dict[str, float] = {}
    all_results: list[dict[str, Any]] = []

    for kw, tfidf, graph, temporal, entity in all_combinations:
        weights = {
            "keyword": kw,
            "tfidf_cosine": tfidf,
            "graph": graph,
            "temporal": temporal,
            "entity": entity,
        }

        score = evaluate_fn(weights) if evaluate_fn else 0.0  # Placeholder - would need actual evaluation data

        all_results.append({"weights": weights, "score": score})

        if score > best_score:
            best_score = score
            best_weights = weights

    return GridSearchResult(
        best_weights=best_weights,
        best_score=best_score,
        all_results=all_results,
    )


def frange(start: float, stop: float, step: float) -> list[float]:
    """Float range generator."""
    result = []
    value = start
    while value <= stop:
        result.append(round(value, 4))
        value += step
    return result


# A/B Testing support
@dataclass
class ABTestConfig:
    """Configuration for A/B testing different weight sets."""

    test_name: str
    control_weights: RRFConfig
    variant_weights: dict[str, list[RRFConfig]]  # variant_name -> [configs]
    traffic_split: dict[str, float]  # variant_name -> percentage

    def validate(self) -> bool:
        """Validate traffic split sums to 100."""
        total = sum(self.traffic_split.values())
        return math.isclose(total, 100.0, abs_tol=0.01)


def select_variant(
    test_config: ABTestConfig,
    user_id: str,
) -> tuple[str, RRFConfig]:
    """
    Select weight variant based on user ID for consistent A/B testing.

    Args:
        test_config: A/B test configuration
        user_id: User ID for consistent assignment

    Returns:
        Tuple of (variant_name, RRFConfig)
    """
    # Hash user ID to get consistent assignment
    hash_value = int(hashlib.sha256(user_id.encode()).hexdigest(), 16)
    roll = (hash_value % 100) + 1

    cumulative = 0.0
    for variant, percentage in test_config.traffic_split.items():
        cumulative += percentage
        if roll <= cumulative:
            # Return first variant config (simplified - would pick based on user segment)
            configs = test_config.variant_weights.get(variant, [test_config.control_weights])
            return (variant, configs[0])

    # Default to control
    return ("control", test_config.control_weights)


def tune_gating_threshold(
    scores: list[float],
    labels: list[bool],
    *,
    min_precision: float = 0.8,
) -> float:
    """Find the optimal score threshold that achieves at least min_precision.

    Args:
        scores: Retrieval scores (e.g. top combined RRF scores).
        labels: Ground truth relevance (True = relevant, False = noise).
        min_precision: Desired precision target.

    Returns:
        Optimal gating threshold rounded down to 4 decimals, or
        ``GATE_ALL_THRESHOLD`` when no candidate reaches ``min_precision``:
        gating everything is safer than injecting at a threshold below
        irrelevant scores. When flooring would drop precision below
        ``min_precision``, the original (unrounded) candidate score is
        returned instead, since no 4-decimal value satisfies both bounds.
    """
    if not scores or not labels or len(scores) != len(labels):
        return DEFAULT_GATING_THRESHOLD

    candidate_thresholds = sorted(set(scores))
    best_threshold: float | None = None
    best_f1 = -1.0

    for thresh in candidate_thresholds:
        tp = sum(1 for s, y in zip(scores, labels, strict=False) if s >= thresh and y)
        fp = sum(1 for s, y in zip(scores, labels, strict=False) if s >= thresh and not y)
        fn = sum(1 for s, y in zip(scores, labels, strict=False) if s < thresh and y)

        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * prec * rec) / (prec + rec) if (prec + rec) > 0 else 0.0

        if prec >= min_precision and f1 > best_f1:
            best_f1 = f1
            best_threshold = thresh

    if best_threshold is None:
        return GATE_ALL_THRESHOLD
    # Round down: rounding to nearest can lift the threshold above the score
    # it was selected for and gate out that very positive. Then re-check the
    # precision target at the rounded value — flooring can also admit a known
    # irrelevant score just below the selected one.
    rounded = math.floor(best_threshold * 10000) / 10000
    tp = sum(1 for s, y in zip(scores, labels, strict=False) if s >= rounded and y)
    fp = sum(1 for s, y in zip(scores, labels, strict=False) if s >= rounded and not y)
    if tp + fp == 0 or tp / (tp + fp) >= min_precision:
        return rounded
    return best_threshold
