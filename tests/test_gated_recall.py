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


def _run_prompt_hook(capsys, inject_resp):
    """Drive the real hook prompt branch with a patched inject response."""
    with (
        patch.object(foresight_hook, "GATED_RECALL_ENABLED", True),
        patch.object(foresight_hook, "GATED_RECALL_THRESHOLD", 0.015),
        patch.object(foresight_hook, "_cache_append"),
        patch.object(foresight_hook, "_post", return_value=(200, json.dumps(inject_resp).encode("utf-8"))),
    ):
        foresight_hook.main(
            ["prompt"],
            json.dumps({"hook_event_name": "UserPromptSubmit", "prompt": "what did we decide?"}),
        )
    return capsys.readouterr().out


def test_hook_gated_recall_below_threshold(capsys):
    inject_resp = {
        "formatted": "Full memory injection content that should be suppressed.",
        "top_score": 0.010,
        "static_and_safety": "[static]\n- [user_preferences] Dark mode only.",
        "memories": [{"memory_id": "m1", "combined_score": 0.010}],
    }

    out = _run_prompt_hook(capsys, inject_resp)

    assert "[FORESIGHT CONTINUITY CONTEXT]" in out
    assert "Dark mode only" in out
    assert foresight_hook.REMINDER_LINE in out
    assert "Full memory injection content that should be suppressed" not in out


def test_hook_injected_recall_above_threshold(capsys):
    inject_resp = {
        "formatted": "Important past context: DB is postgres 17.",
        "top_score": 0.035,
        "static_and_safety": "[static]\n- [user_preferences] Dark mode only.",
        "memories": [{"memory_id": "m1", "combined_score": 0.035}],
    }

    out = _run_prompt_hook(capsys, inject_resp)

    assert "Important past context: DB is postgres 17." in out
    assert foresight_hook.REMINDER_LINE not in out


def test_hook_postcompact_applies_score_gate(capsys):
    """A low-score hit must not re-enter the prompt through PostCompact."""
    inject_resp = {
        "formatted": "LOW SCORE CONTENT",
        "top_score": 0.010,
        "static_and_safety": "[static]\n- keep safety lane",
        "memories": [{"memory_id": "m1", "combined_score": 0.010}],
    }
    with (
        patch.object(foresight_hook, "GATED_RECALL_ENABLED", True),
        patch.object(foresight_hook, "GATED_RECALL_THRESHOLD", 0.015),
        patch.object(foresight_hook, "_cache_append"),
        patch.object(foresight_hook, "_cache_recent", return_value=[]),
        patch.object(foresight_hook, "_post", return_value=(200, json.dumps(inject_resp).encode("utf-8"))),
    ):
        foresight_hook.main(
            ["postcompact"],
            json.dumps({"hook_event_name": "PostCompact", "summary": "we discussed the migration"}),
        )
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "LOW SCORE CONTENT" not in ctx
    assert "keep safety lane" in ctx
    assert foresight_hook.REMINDER_LINE in ctx


def test_hook_stop_with_message_dicts_emits_nudge(capsys, tmp_path):
    """Cache-mode Stop replaces ``tail`` with message dicts; the nudge must not crash."""
    payload = {
        "hook_event_name": "Stop",
        "messages": [{"role": "assistant", "content": "We decided to use Redis for session caching."}],
    }
    with (
        patch.object(foresight_hook, "MODE", "cache"),
        patch.object(foresight_hook, "CACHE_DIR", str(tmp_path)),
        patch.object(foresight_hook, "_cache_append"),
        patch.object(foresight_hook, "_transcript_tail", return_value=[]),
    ):
        foresight_hook.main(["stop"], json.dumps(payload))
    out = capsys.readouterr().out
    assert json.loads(out)["systemMessage"] == foresight_hook.WRITE_BACK_NUDGE


def test_write_back_nudge_emitted_once_per_content(monkeypatch, tmp_path):
    """The same transcript tail must not re-emit the nudge on every Stop."""
    monkeypatch.setattr(foresight_hook, "CACHE_DIR", str(tmp_path))
    texts = ["we decided to use uv for python"]

    assert not foresight_hook._nudge_already_emitted("s1", texts)
    foresight_hook._mark_nudge_emitted("s1", texts)

    assert foresight_hook._nudge_already_emitted("s1", texts)
    assert not foresight_hook._nudge_already_emitted("s1", [*texts, "and fixed the bug"])


def test_hook_scrubs_base64_api_key_tail():
    """An unquoted base64 value must be scrubbed whole, not cut at '/', '+' or '='."""
    raw = "api_" + "key=" + "A" * 21 + "==/" + "qrstuv" + "+wx"

    scrubbed = foresight_hook._scrub_secrets(raw)

    assert "qrstuv" not in scrubbed
    assert "[REDACTED-SECRET]" in scrubbed


def test_hook_scrubs_large_private_key():
    """A PEM body past the old 4,000-char cap must not reach the transcript."""
    # Assembled at runtime: the file never holds a complete key literal for a
    # secret-scanning job to flag.
    body = "MIIE" + "A" * 6000
    header = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
    footer = "-----END " + "RSA PRIVATE KEY" + "-----"
    raw = f"{header}\n{body}\n{footer}"

    scrubbed = foresight_hook._scrub_secrets(f"key: {raw} end")

    assert body not in scrubbed
    assert "[REDACTED-SECRET]" in scrubbed


def test_hook_scrubs_compound_and_quoted_credentials():
    """Compound keys and quoted values must not survive the scrubber."""
    # Assembled from fragments so the file holds no assignment-shaped literal
    # for a secret-scanning job to flag.
    secret = "abcdef" + "1234567890" + "abcdef"
    pw_value = "pw" + "-" + "notarealvalue"
    samples = (
        "DB_" + "PASSWORD" + '="' + pw_value + '"',
        '{"api_' + 'key": "' + secret + '"}',
        "FORESIGHT_API_" + "KEY='" + secret + "'",
    )
    for raw in samples:
        scrubbed = foresight_hook._scrub_secrets(raw)
        assert secret not in scrubbed
        assert pw_value not in scrubbed
        assert "[REDACTED-SECRET]" in scrubbed


def test_hook_scrubs_messages_before_transport():
    """Direct-mode transport must carry redacted text, never a raw credential."""
    raw_token = "gh" + "p_" + "1234567890abcdef1234567890abcdef1234"
    payload = {
        "hook_event_name": "Stop",
        "messages": [{"role": "assistant", "content": f"token {raw_token}"}],
    }
    posted = []

    def _fake_post(url, body, timeout):
        _ = timeout
        posted.append((url, body))
        return 200, b"{}"

    with (
        patch.object(foresight_hook, "MODE", "direct"),
        patch.object(foresight_hook, "_post", side_effect=_fake_post),
    ):
        foresight_hook.main(["stop"], json.dumps(payload))

    transcript_calls = [body for url, body in posted if url.endswith("/session/transcript")]
    assert transcript_calls, "stop hook should post the transcript"
    assert raw_token not in json.dumps(transcript_calls)


def test_storable_fact_lines_across_separate_messages():
    """A heading in message 1 and body in message 2 must capture the body in storable lines."""
    messages = [
        "Architectural Decision:",
        "We are adopting PostgreSQL 17 for primary persistence.",
    ]
    lines = foresight_hook._storable_fact_lines(messages)
    assert "Architectural Decision:" in lines
    assert "We are adopting PostgreSQL 17 for primary persistence." in lines


def test_hook_scrubs_footerless_pem_short_final_line():
    """A footerless PEM with a short final base64 line must be scrubbed completely."""
    header = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
    body_line1 = "A" * 64
    body_line2 = "ABCD"
    raw = f"config:\n{header}\n{body_line1}\n{body_line2}\ntrailing"
    scrubbed = foresight_hook._scrub_secrets(raw)
    assert body_line1 not in scrubbed
    assert body_line2 not in scrubbed
    assert "trailing" in scrubbed
    assert "[REDACTED-SECRET]" in scrubbed
