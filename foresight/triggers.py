"""L4: JIT Trigger Engine — rolling window threshold logic + EventBus wiring."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from .event_bus import EventBus, EventType, Event


@dataclass(frozen=True, slots=True)
class CaseFlag:
    """A single clinical flag event for a clinician."""

    clinician_id: str
    flag_type: str
    severity: float
    timestamp: datetime
    source: str


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    """Result of rolling window evaluation."""

    should_trigger: bool
    matching_flags: int
    clinician_id: str | None = None


class JITTriggerEngine:
    """Evaluates rolling windows of case flags to trigger JIT scenario top-ups."""

    def __init__(self, event_bus: EventBus | None = None):
        self._event_bus = event_bus
        self._flags: list[CaseFlag] = []
        if event_bus:
            self._subscribe_to_events()

    def _subscribe_to_events(self) -> None:
        """Subscribe to bias and crisis events from EventBus."""
        if not self._event_bus:
            return
        # Bias events
        self._event_bus.subscribe(EventType.BIAS_DETECTED, self._on_bias_event)
        self._event_bus.subscribe(EventType.BIAS_THRESHOLD_EXCEEDED, self._on_bias_event)
        # Crisis events
        self._event_bus.subscribe(EventType.CRISIS_DETECTED, self._on_crisis_event)
        self._event_bus.subscribe(EventType.CRISIS_THRESHOLD_EXCEEDED, self._on_crisis_event)

    def _on_bias_event(self, event: Event) -> None:
        """Convert bias event to CaseFlag."""
        payload = event.payload
        clinician_id = payload.get("clinician_id") or payload.get("user_id") or "unknown"
        flag = CaseFlag(
            clinician_id=clinician_id,
            flag_type="bias",
            severity=payload.get("bias_score") or payload.get("overall_bias_score", 0.0),
            timestamp=event.timestamp,
            source="bias_detection",
        )
        self._flags.append(flag)

    def _on_crisis_event(self, event: Event) -> None:
        """Convert crisis event to CaseFlag."""
        payload = event.payload
        clinician_id = payload.get("clinician_id") or payload.get("user_id") or "unknown"
        flag = CaseFlag(
            clinician_id=clinician_id,
            flag_type="crisis",
            severity=payload.get("crisis_score") or payload.get("overall_bias_score", 1.0),
            timestamp=event.timestamp,
            source="crisis_detection",
        )
        self._flags.append(flag)

    def rolling_window(
        self,
        events: list[CaseFlag] | None = None,
        window: timedelta = timedelta(days=7),
        threshold: int = 3,
    ) -> dict[str, TriggerDecision]:
        """Count flags within time window per clinician; trigger if >= threshold.

        Returns one TriggerDecision per clinician so thresholds evaluate
        independently per clinician. If events is None, uses internally
        accumulated flags from EventBus.
        """
        flags = events if events is not None else self._flags
        if not flags:
            return {}

        by_clinician: dict[str, list[CaseFlag]] = defaultdict(list)
        for flag in flags:
            by_clinician[flag.clinician_id].append(flag)

        decisions: dict[str, TriggerDecision] = {}
        for clinician_id, clinician_flags in by_clinician.items():
            now = datetime.now(clinician_flags[0].timestamp.tzinfo)
            cutoff = now - window
            matching = [e for e in clinician_flags if e.timestamp >= cutoff]
            decisions[clinician_id] = TriggerDecision(
                should_trigger=len(matching) >= threshold,
                matching_flags=len(matching),
                clinician_id=clinician_id,
            )
        return decisions
