"""L4: JIT Trigger Engine — rolling window threshold logic.

Test asserts JITTriggerEngine.rolling_window correctly evaluates
CaseFlag sequences against threshold.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from foresight.triggers import CaseFlag, JITTriggerEngine, TriggerDecision


def _flag(
    clinician_id: str,
    flag_type: str,
    severity: float,
    hours_ago: int,
    source: str = "bias_detection",
) -> CaseFlag:
    return CaseFlag(
        clinician_id=clinician_id,
        flag_type=flag_type,
        severity=severity,
        timestamp=datetime.now(UTC) - timedelta(hours=hours_ago),
        source=source,
    )


def test_rolling_window_below_threshold_returns_no_trigger() -> None:
    """Flags within window but below threshold → no trigger."""
    engine = JITTriggerEngine()
    flags = [
        _flag("clin-1", "bias_high", 0.7, 1),
        _flag("clin-1", "bias_high", 0.8, 3),
    ]
    decisions = engine.rolling_window(flags, window=timedelta(days=7), threshold=3)
    assert decisions["clin-1"].should_trigger is False
    assert decisions["clin-1"].matching_flags == 2


def test_rolling_window_at_threshold_returns_trigger() -> None:
    """Flags at threshold → trigger fires."""
    engine = JITTriggerEngine()
    flags = [
        _flag("clin-1", "bias_high", 0.7, 1),
        _flag("clin-1", "bias_high", 0.8, 3),
        _flag("clin-1", "crisis", 0.9, 5),
    ]
    decisions = engine.rolling_window(flags, window=timedelta(days=7), threshold=3)
    assert decisions["clin-1"].should_trigger is True
    assert decisions["clin-1"].matching_flags == 3
    assert decisions["clin-1"].clinician_id == "clin-1"


def test_rolling_window_excludes_expired_flags() -> None:
    """Flags outside window excluded from count."""
    engine = JITTriggerEngine()
    flags = [
        _flag("clin-1", "bias_high", 0.7, 1),  # inside
        _flag("clin-1", "bias_high", 0.8, 3),  # inside
        _flag("clin-1", "bias_high", 0.9, 10 * 24),  # outside 7-day window
    ]
    decisions = engine.rolling_window(flags, window=timedelta(days=7), threshold=3)
    assert decisions["clin-1"].should_trigger is False
    assert decisions["clin-1"].matching_flags == 2


def test_rolling_window_per_clinician_isolation() -> None:
    """Flags for different clinicians evaluated independently."""
    engine = JITTriggerEngine()
    flags = [
        _flag("clin-1", "bias_high", 0.7, 1),
        _flag("clin-1", "bias_high", 0.8, 3),
        _flag("clin-1", "bias_high", 0.9, 5),  # clin-1 at threshold
        _flag("clin-2", "bias_high", 0.7, 1),
        _flag("clin-2", "bias_high", 0.8, 3),  # clin-2 below threshold
    ]
    decisions = engine.rolling_window(flags, window=timedelta(days=7), threshold=3)
    assert decisions["clin-1"].should_trigger is True
    assert decisions["clin-1"].matching_flags == 3
    assert decisions["clin-2"].should_trigger is False
    assert decisions["clin-2"].matching_flags == 2


def test_rolling_window_empty_flags_returns_empty_dict() -> None:
    """No flags → no decisions."""
    engine = JITTriggerEngine()
    assert engine.rolling_window([]) == {}


def test_caseflag_fields_present() -> None:
    """CaseFlag dataclass has expected fields."""
    f = CaseFlag(
        clinician_id="clin-1",
        flag_type="bias_high",
        severity=0.8,
        timestamp=datetime.now(UTC),
        source="bias_detection",
    )
    assert f.clinician_id == "clin-1"
    assert f.flag_type == "bias_high"
    assert f.severity == 0.8
    assert f.source == "bias_detection"
    assert f.timestamp is not None
