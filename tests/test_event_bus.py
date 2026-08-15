from __future__ import annotations

from datetime import datetime, timezone

import foresight.event_bus as event_bus_module
from foresight.event_bus import Event, EventType, get_event_bus, reset_event_bus


class _FakeStore:
    def append(self, event: Event) -> None:
        self.last_event = event


def _make_event() -> Event:
    return Event(
        id="evt-stream",
        event_type=EventType.MEMORY_STORED,
        timestamp=datetime.now(timezone.utc),
        actor="tester",
        entity_id="memory-1",
        payload={"value": "hello"},
    )


def test_event_bus_publish_persists_event(monkeypatch):
    """Event bus should persist events through the store."""
    reset_event_bus()
    monkeypatch.setattr(event_bus_module, "EventStore", _FakeStore)

    bus = get_event_bus()
    event = _make_event()
    bus.publish(event)

    # Singleton should return the same instance
    assert get_event_bus() is bus

    reset_event_bus()
