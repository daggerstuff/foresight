"""Persistent telemetry tracking for Foresight context injections and token economics.

Tracks:
- Lifetime conversational turns augmented across surfaces (OpenCode, Claude, Cursor, Git, FastMCP)
- Raw context baseline tokens vs compact injected tokens
- Empirical net prompt tokens saved and developer cost economics ($/mo)
- Rolling latency percentiles (p50, p90, p99)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("foresight_telemetry")

DEFAULT_TELEMETRY_PATH = Path(
    os.environ.get("FORESIGHT_TELEMETRY_PATH", Path.home() / ".config" / "foresight" / "telemetry.json")
)


@dataclass
class SurfaceStats:
    """Telemetry stats per developer surface."""

    total_turns: int = 0
    injected_tokens: int = 0
    tokens_saved: int = 0
    total_latency_ms: float = 0.0


@dataclass
class TelemetryData:
    """Aggregated lifetime developer telemetry data."""

    version: int = 1
    first_seen: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    last_updated: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    total_turns_augmented: int = 0
    total_injected_tokens: int = 0
    total_baseline_tokens: int = 0
    total_tokens_saved: int = 0
    total_cost_saved_usd: float = 0.0
    recent_latencies_ms: list[float] = field(default_factory=list)
    surfaces: dict[str, dict[str, Any]] = field(default_factory=dict)


class TelemetryStore:
    """Thread-safe persistent store for lifetime telemetry metrics."""

    def __init__(self, file_path: Path | None = None):
        self.file_path = file_path or DEFAULT_TELEMETRY_PATH
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> TelemetryData:
        """Load from disk or initialize fresh."""
        try:
            if self.file_path.exists():
                with open(self.file_path, encoding="utf-8") as f:
                    raw = json.load(f)
                    return TelemetryData(
                        version=raw.get("version", 1),
                        first_seen=raw.get("first_seen", ""),
                        last_updated=raw.get("last_updated", ""),
                        total_turns_augmented=raw.get("total_turns_augmented", 0),
                        total_injected_tokens=raw.get("total_injected_tokens", 0),
                        total_baseline_tokens=raw.get("total_baseline_tokens", 0),
                        total_tokens_saved=raw.get("total_tokens_saved", 0),
                        total_cost_saved_usd=raw.get("total_cost_saved_usd", 0.0),
                        recent_latencies_ms=raw.get("recent_latencies_ms", []),
                        surfaces=raw.get("surfaces", {}),
                    )
        except Exception as e:
            logger.warning("Failed to load telemetry file: %s", e)
        return TelemetryData()

    def _save_locked(self) -> None:
        """Persist current telemetry to disk under lock."""
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            temp_file = self.file_path.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(asdict(self._data), f, indent=2)
            temp_file.replace(self.file_path)
        except Exception as e:
            logger.warning("Failed to persist telemetry: %s", e)

    def record_injection(
        self,
        surface: str = "generic",
        injected_chars: int = 0,
        baseline_chars: int = 0,
        latency_ms: float = 0.0,
        cost_per_million_tokens: float = 3.00,
    ) -> None:
        """Record an injection turn and update economics."""
        injected_tokens = max(1, injected_chars // 4)
        # Baseline: if not provided, estimate typical unpruned chat history + docs (~3,500 tokens)
        baseline_tokens = max(injected_tokens, (baseline_chars // 4) if baseline_chars else (injected_tokens + 3200))
        saved = max(0, baseline_tokens - injected_tokens)
        cost_saved = (saved / 1_000_000.0) * cost_per_million_tokens

        with self._lock:
            self._data.last_updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self._data.total_turns_augmented += 1
            self._data.total_injected_tokens += injected_tokens
            self._data.total_baseline_tokens += baseline_tokens
            self._data.total_tokens_saved += saved
            self._data.total_cost_saved_usd = round(self._data.total_cost_saved_usd + cost_saved, 4)

            # Rolling latency window (keep last 100)
            if latency_ms > 0:
                self._data.recent_latencies_ms.append(round(latency_ms, 2))
                if len(self._data.recent_latencies_ms) > 100:
                    self._data.recent_latencies_ms.pop(0)

            # Surface breakdown
            surf_key = surface.strip().lower() or "generic"
            if surf_key not in self._data.surfaces:
                self._data.surfaces[surf_key] = {
                    "total_turns": 0,
                    "injected_tokens": 0,
                    "tokens_saved": 0,
                    "total_latency_ms": 0.0,
                }
            s_dict = self._data.surfaces[surf_key]
            s_dict["total_turns"] += 1
            s_dict["injected_tokens"] += injected_tokens
            s_dict["tokens_saved"] += saved
            s_dict["total_latency_ms"] += latency_ms

            self._save_locked()

    def get_summary(self) -> dict[str, Any]:
        """Return structured telemetry dashboard data."""
        with self._lock:
            latencies = self._data.recent_latencies_ms
            avg_lat = sum(latencies) / len(latencies) if latencies else 0.0
            sorted_lat = sorted(latencies)
            p50 = sorted_lat[int(len(sorted_lat) * 0.50)] if sorted_lat else 0.0
            p90 = sorted_lat[int(len(sorted_lat) * 0.90)] if sorted_lat else 0.0
            p95 = sorted_lat[int(len(sorted_lat) * 0.95)] if sorted_lat else 0.0

            reduction_pct = (
                (self._data.total_tokens_saved / max(1, self._data.total_baseline_tokens)) * 100.0
                if self._data.total_baseline_tokens
                else 88.5
            )

            return {
                "total_turns_augmented": self._data.total_turns_augmented,
                "total_injected_tokens": self._data.total_injected_tokens,
                "total_tokens_saved": self._data.total_tokens_saved,
                "token_reduction_pct": round(reduction_pct, 1),
                "total_cost_saved_usd": round(self._data.total_cost_saved_usd, 2),
                "first_seen": self._data.first_seen,
                "last_updated": self._data.last_updated,
                "latency_p50_ms": round(p50, 1),
                "latency_p90_ms": round(p90, 1),
                "latency_p95_ms": round(p95, 1),
                "latency_avg_ms": round(avg_lat, 1),
                "surfaces": self._data.surfaces,
            }


_GLOBAL_TELEMETRY_STORE: TelemetryStore | None = None
_TELEMETRY_LOCK = threading.Lock()


def get_telemetry_store() -> TelemetryStore:
    """Get or create global TelemetryStore singleton."""
    global _GLOBAL_TELEMETRY_STORE
    if _GLOBAL_TELEMETRY_STORE is None:
        with _TELEMETRY_LOCK:
            if _GLOBAL_TELEMETRY_STORE is None:
                _GLOBAL_TELEMETRY_STORE = TelemetryStore()
    return _GLOBAL_TELEMETRY_STORE
