"""L4: JIT Trigger Engine — rolling window threshold logic.

Test asserts JITTriggerEngine.rolling_window correctly evaluates
CaseFlag sequences against threshold.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from foresight.triggers import CaseFlag, CaseFlagStore, JITTriggerEngine, TriggerDecision


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


def test_caseflag_store_persists_flags() -> None:
    """CaseFlagStore saves and loads flags from SQLite."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_flags.db")
        store = CaseFlagStore(db_path=db_path)

        flag1 = _flag("clin-1", "bias", 0.7, 1)
        flag2 = _flag("clin-2", "crisis", 0.9, 2)

        store.save(flag1)
        store.save(flag2)

        loaded = store.load()
        assert len(loaded) == 2
        clinician_ids = {f.clinician_id for f in loaded}
        assert clinician_ids == {"clin-1", "clin-2"}


def test_caseflag_store_filters_by_clinician() -> None:
    """CaseFlagStore can filter flags by clinician_id."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_flags.db")
        store = CaseFlagStore(db_path=db_path)

        store.save(_flag("clin-1", "bias", 0.7, 1))
        store.save(_flag("clin-1", "crisis", 0.8, 2))
        store.save(_flag("clin-2", "bias", 0.6, 3))

        clin1_flags = store.load(clinician_id="clin-1")
        assert len(clin1_flags) == 2
        assert all(f.clinician_id == "clin-1" for f in clin1_flags)


def test_caseflag_store_filters_by_time() -> None:
    """CaseFlagStore can filter flags by timestamp."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_flags.db")
        store = CaseFlagStore(db_path=db_path)
        store.save(_flag("clin-1", "bias", 0.7, 10))
        store.save(_flag("clin-1", "bias", 0.8, 2))
        store.save(_flag("clin-1", "bias", 0.9, 1))

        since = datetime.now(UTC) - timedelta(hours=5)
        recent = store.load(since=since)
        assert len(recent) == 2


def test_caseflag_store_clear() -> None:
    """CaseFlagStore can clear flags."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_flags.db")
        store = CaseFlagStore(db_path=db_path)

        store.save(_flag("clin-1", "bias", 0.7, 1))
        store.save(_flag("clin-2", "bias", 0.8, 2))

        deleted = store.clear(clinician_id="clin-1")
        assert deleted == 1

        remaining = store.load()
        assert len(remaining) == 1
        assert remaining[0].clinician_id == "clin-2"


def test_engine_loads_persisted_flags_on_init() -> None:
    """JITTriggerEngine loads persisted flags when store is provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_flags.db")
        store = CaseFlagStore(db_path=db_path)
        store.save(_flag("clin-1", "bias", 0.7, 1))
        store.save(_flag("clin-1", "bias", 0.8, 2))
        store.save(_flag("clin-1", "bias", 0.9, 3))
        engine = JITTriggerEngine(store=store)
        assert len(engine._flags) == 3
        decisions = engine.rolling_window(window=timedelta(days=7), threshold=3)
        assert decisions["clin-1"].should_trigger is True
        assert decisions["clin-1"].matching_flags == 3


def test_engine_persists_flags_from_events() -> None:
    """JITTriggerEngine persists flags received from EventBus."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_flags.db")
        store = CaseFlagStore(db_path=db_path)
        engine = JITTriggerEngine(store=store)
        from foresight.event_bus import Event, EventType

        event1 = Event(
            id="evt-1",
            event_type=EventType.BIAS_DETECTED,
            actor="clin-1",
            entity_id="session-1",
            payload={"clinician_id": "clin-1", "bias_score": 0.7},
            timestamp=datetime.now(UTC),
        )
        event2 = Event(
            id="evt-2",
            event_type=EventType.CRISIS_DETECTED,
            actor="clin-1",
            entity_id="session-1",
            payload={"clinician_id": "clin-1", "crisis_score": 0.9},
            timestamp=datetime.now(UTC),
        )
        engine._on_bias_event(event1)
        engine._on_crisis_event(event2)
        loaded = store.load()
        assert len(loaded) == 2
        assert loaded[0].flag_type == "bias"
        assert loaded[1].flag_type == "crisis"
