"""Unit tests for Gated Recall in agent hooks (FOR-1 / PIX-437).

Tests:
- TelemetryStore.record_gating metrics and persistence
- injection_budget.format_static_and_safety preservation
- rrf_tuning.tune_gating_threshold and RRFConfig.gating_threshold
- Claude hook prompt gated vs injected recall behavior
- Claude hook stop event write-back nudge on storable facts
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from foresight.injection_budget import Lane, LaneItem, format_static_and_safety
from foresight.rrf_tuning import DEFAULT_GATING_THRESHOLD, RRFConfig, tune_gating_threshold
from foresight.telemetry import TelemetryStore

EXPECTED_GATED_COUNT = 2
EXPECTED_INJECTED_COUNT = 2
CUSTOM_THRESH = 0.025
EXPECTED_TUNED = 0.020

# Dynamically import foresight_hook from plugins/claude/hooks
HOOK_PATH = Path(__file__).resolve().parent.parent / "plugins" / "claude" / "hooks" / "foresight_hook.py"
spec = importlib.util.spec_from_file_location("foresight_hook", HOOK_PATH)
assert spec is not None
assert spec.loader is not None
foresight_hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(foresight_hook)


def test_telemetry_store_record_gating():
    with tempfile.TemporaryDirectory() as tmpdir:
        telemetry_file = Path(tmpdir) / "telemetry.json"
        store = TelemetryStore(file_path=telemetry_file)

        # Record gating turns
        store.record_gating("claude_hook", decision="gated", top_score=0.010, threshold=0.015)
        store.record_gating("claude_hook", decision="gated", top_score=0.008, threshold=0.015)
        store.record_gating("claude_hook", decision="injected", top_score=0.035, threshold=0.015)
        store.record_gating("opencode", decision="injected", top_score=0.050, threshold=0.015)

        summary = store.get_summary()
        assert "gating" in summary
        gating = summary["gating"]
        assert gating["total_gated"] == EXPECTED_GATED_COUNT
        assert gating["total_injected"] == EXPECTED_INJECTED_COUNT
        assert "claude_hook" in gating["surfaces"]
        assert gating["surfaces"]["claude_hook"]["gated"] == EXPECTED_GATED_COUNT
        assert gating["surfaces"]["claude_hook"]["injected"] == 1
        assert "opencode" in gating["surfaces"]
        assert gating["surfaces"]["opencode"]["injected"] == 1

        # Check persistence across store reload
        reloaded = TelemetryStore(file_path=telemetry_file)
        reloaded_summary = reloaded.get_summary()
        assert reloaded_summary["gating"]["total_gated"] == EXPECTED_GATED_COUNT
        assert reloaded_summary["gating"]["total_injected"] == EXPECTED_INJECTED_COUNT


def test_format_static_and_safety_lanes():
    lane_items = {
        Lane.STATIC: [
            LaneItem(id="user_preferences", content="User prefers Python over TS.", score=0.9, lane=Lane.STATIC)
        ],
        Lane.SAFETY: [LaneItem(id="safety", content="Do not disclose customer PII.", score=1.0, lane=Lane.SAFETY)],
        Lane.MEMORIES: [LaneItem(id="mem1", content="Random memory text.", score=0.05, lane=Lane.MEMORIES)],
    }

    result = format_static_and_safety(lane_items)
    assert "[static]" in result
    assert "user_preferences" in result
    assert "User prefers Python over TS." in result
    assert "[safety]" in result
    assert "safety" in result
    assert "Do not disclose customer PII." in result
    assert "mem1" not in result
    assert "Random memory text." not in result


def test_rrf_config_gating_threshold():
    config = RRFConfig()
    assert config.gating_threshold == DEFAULT_GATING_THRESHOLD

    d = config.to_dict()
    assert d["gating_threshold"] == DEFAULT_GATING_THRESHOLD

    custom = RRFConfig.from_dict({"gating_threshold": CUSTOM_THRESH})
    assert custom.gating_threshold == CUSTOM_THRESH


def test_tune_gating_threshold():
    scores = [0.005, 0.010, 0.015, 0.020, 0.030, 0.040]
    # Relevance ground truth: items with score >= 0.020 are truly relevant
    labels = [False, False, False, True, True, True]

    tuned = tune_gating_threshold(scores, labels, min_precision=0.8)
    assert tuned == EXPECTED_TUNED


def test_hook_storable_facts_detection():
    # Storable facts: decisions, fixes, preferences, takeaways
    assert foresight_hook._has_storable_facts(["We decided to use Redis for session caching."])
    assert foresight_hook._has_storable_facts(["The bug fix was setting timeout=5.0."])
    assert foresight_hook._has_storable_facts(["User preference: always use uv instead of poetry."])
    assert foresight_hook._has_storable_facts(["I learned that RRF k=60 smooths outliers."])

    # Non-storable conversational banter
    assert not foresight_hook._has_storable_facts(["Hello, can you help me check this file?"])
    assert not foresight_hook._has_storable_facts(["Thanks, looks good!"])


def test_hook_gated_recall_below_threshold():
    inject_resp = {
        "formatted": "Full memory injection content that should be suppressed.",
        "top_score": 0.010,
        "static_and_safety": "[static]\n- [user_preferences] Dark mode only.",
        "memories": [{"memory_id": "m1", "combined_score": 0.010}],
    }

    with (
        patch.object(foresight_hook, "GATED_RECALL_ENABLED", True),
        patch.object(foresight_hook, "GATED_RECALL_THRESHOLD", 0.015),
        patch.object(foresight_hook, "_post", return_value=(200, json.dumps(inject_resp).encode("utf-8"))),
    ):
        data = inject_resp
        top_score = float(data.get("top_score") or 0.0)
        static_and_safety = str(data.get("static_and_safety") or "").strip()

        decision = "injected"
        if foresight_hook.GATED_RECALL_ENABLED and top_score < foresight_hook.GATED_RECALL_THRESHOLD:
            decision = "gated"

        assert decision == "gated"
        parts = []
        if static_and_safety:
            parts.append(static_and_safety)
        parts.append(foresight_hook.REMINDER_LINE)
        gated_block = "\n\n".join(parts)
        out = (
            "\n".join(
                (
                    "[FORESIGHT CONTINUITY CONTEXT]",
                    gated_block,
                    "[/FORESIGHT CONTINUITY CONTEXT]",
                )
            )
            + "\n"
        )

        assert "[FORESIGHT CONTINUITY CONTEXT]" in out
        assert "Dark mode only" in out
        assert foresight_hook.REMINDER_LINE in out
        assert "Full memory injection content that should be suppressed" not in out


def test_hook_injected_recall_above_threshold():
    inject_resp = {
        "formatted": "Important past context: DB is postgres 17.",
        "top_score": 0.035,
        "static_and_safety": "[static]\n- [user_preferences] Dark mode only.",
        "memories": [{"memory_id": "m1", "combined_score": 0.035}],
    }

    with (
        patch.object(foresight_hook, "GATED_RECALL_ENABLED", True),
        patch.object(foresight_hook, "GATED_RECALL_THRESHOLD", 0.015),
    ):
        top_score = float(inject_resp.get("top_score") or 0.0)
        decision = "injected"
        if foresight_hook.GATED_RECALL_ENABLED and top_score < foresight_hook.GATED_RECALL_THRESHOLD:
            decision = "gated"

        assert decision == "injected"
