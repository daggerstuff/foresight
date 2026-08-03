"""L4: JIT Trigger Engine — rolling window threshold logic + EventBus wiring."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .config import DB_PATH
from .event_bus import EventBus, EventType, Event

if TYPE_CHECKING:
    from .connection_pool import PooledConnection


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


class CaseFlagStore:
    """SQLite/PostgreSQL persistence layer for CaseFlag entries.

    Stores flags keyed by clinician_id so they survive engine restarts.
    Uses the same connection pool as the rest of foresight.
    """

    TABLE = "case_flags"

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._ensure_table()

    def _get_connection(self) -> sqlite3.Connection | PooledConnection:
        from .connection_pool import get_pool

        pool = get_pool(self._db_path)
        return pool.acquire()

    def _ensure_table(self) -> None:
        conn = self._get_connection()
        try:
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    clinician_id TEXT NOT NULL,
                    flag_type TEXT NOT NULL,
                    severity REAL NOT NULL,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL
                )"""
            )
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{self.TABLE}_clinician ON {self.TABLE} (clinician_id)")
            conn.commit()
        finally:
            conn.close()

    def save(self, flag: CaseFlag) -> None:
        """Persist a single CaseFlag."""
        conn = self._get_connection()
        try:
            conn.execute(
                f"INSERT INTO {self.TABLE} (clinician_id, flag_type, severity, timestamp, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    flag.clinician_id,
                    flag.flag_type,
                    flag.severity,
                    flag.timestamp.isoformat(),
                    flag.source,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def save_many(self, flags: list[CaseFlag]) -> None:
        """Persist multiple CaseFlags in a single transaction."""
        if not flags:
            return
        conn = self._get_connection()
        try:
            conn.executemany(
                f"INSERT INTO {self.TABLE} (clinician_id, flag_type, severity, timestamp, source) "
                "VALUES (?, ?, ?, ?, ?)",
                [(f.clinician_id, f.flag_type, f.severity, f.timestamp.isoformat(), f.source) for f in flags],
            )
            conn.commit()
        finally:
            conn.close()

    def load(
        self,
        clinician_id: str | None = None,
        since: datetime | None = None,
    ) -> list[CaseFlag]:
        """Load persisted flags, optionally filtered by clinician and time."""
        conn = self._get_connection()
        try:
            query = f"SELECT clinician_id, flag_type, severity, timestamp, source FROM {self.TABLE}"
            params: list = []
            clauses: list[str] = []
            if clinician_id is not None:
                clauses.append("clinician_id = ?")
                params.append(clinician_id)
            if since is not None:
                clauses.append("timestamp >= ?")
                params.append(since.isoformat())
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY timestamp"
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        return [
            CaseFlag(
                clinician_id=row[0],
                flag_type=row[1],
                severity=row[2],
                timestamp=datetime.fromisoformat(row[3]),
                source=row[4],
            )
            for row in rows
        ]

    def clear(self, clinician_id: str | None = None) -> int:
        """Delete persisted flags. If clinician_id given, only that clinician's."""
        conn = self._get_connection()
        try:
            if clinician_id is not None:
                cursor = conn.execute(f"DELETE FROM {self.TABLE} WHERE clinician_id = ?", (clinician_id,))
            else:
                cursor = conn.execute(f"DELETE FROM {self.TABLE}")
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()


class JITTriggerEngine:
    """Evaluates rolling windows of case flags to trigger JIT scenario top-ups."""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        store: CaseFlagStore | None = None,
    ):
        self._event_bus = event_bus
        self._store = store
        self._flags: list[CaseFlag] = []
        if self._store is not None:
            self._flags = self._store.load()
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
        if self._store is not None:
            self._store.save(flag)

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
        if self._store is not None:
            self._store.save(flag)

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
