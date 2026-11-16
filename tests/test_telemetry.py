"""Unit tests for Foresight lifetime telemetry and token economics tracker."""

from __future__ import annotations

import tempfile
from pathlib import Path

from foresight.telemetry import TelemetryStore

# A one-line injection payload replaces a much larger baseline payload; anything
# below this reduction means the accounting regressed.
MIN_TOKEN_REDUCTION_PCT = 80.0


def test_telemetry_recording_and_economics():
    with tempfile.TemporaryDirectory() as tmpdir:
        telemetry_file = Path(tmpdir) / "telemetry.json"
        store = TelemetryStore(file_path=telemetry_file)

        # Record 3 injection turns
        turns = (
            {"surface": "opencode", "injected_chars": 400, "baseline_chars": 14000, "latency_ms": 12.5},
            {"surface": "claude", "injected_chars": 800, "baseline_chars": 16000, "latency_ms": 15.0},
            {"surface": "mcp", "injected_chars": 200, "baseline_chars": 12000, "latency_ms": 2.0},
        )
        for turn in turns:
            store.record_injection(**turn)

        summary = store.get_summary()
        assert summary["total_turns_augmented"] == len(turns)
        assert summary["total_injected_tokens"] > 0
        assert summary["total_tokens_saved"] > 0
        assert summary["token_reduction_pct"] > MIN_TOKEN_REDUCTION_PCT
        assert summary["total_cost_saved_usd"] >= 0.0
        assert "opencode" in summary["surfaces"]
        assert "claude" in summary["surfaces"]
        assert "mcp" in summary["surfaces"]

        # Test persistence across reloads
        reloaded_store = TelemetryStore(file_path=telemetry_file)
        reloaded_summary = reloaded_store.get_summary()
        assert reloaded_summary["total_turns_augmented"] == len(turns)
        assert reloaded_summary["total_tokens_saved"] == summary["total_tokens_saved"]
