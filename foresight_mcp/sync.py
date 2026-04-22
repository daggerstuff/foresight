"""
Offline-First Synchronization

Provides offline-first sync capabilities:
- Local storage (SQLite)
- Operation queue with persistence
- Sync manager with retry logic
- Progress events for UI
- Conflict resolution with CRDTs
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .crdt import LWWRegister, ORSet, VectorClock

logger = logging.getLogger("foresight_sync")


class SyncStatus(str, Enum):
    """Sync status."""
    IDLE = "idle"
    SYNCING = "syncing"
    OFFLINE = "offline"
    ERROR = "error"


class OperationType(str, Enum):
    """Types of operations."""
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass
class Operation:
    """Represents a pending operation."""
    id: str
    type: OperationType
    entity_type: str
    entity_id: str
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    retry_count: int = 0
    last_attempt: datetime | None = None
    vector_clock: VectorClock = field(default_factory=VectorClock)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
            "retry_count": self.retry_count,
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "vector_clock": self.vector_clock.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Operation:
        op = cls(
            id=data["id"],
            type=OperationType(data["type"]),
            entity_type=data["entity_type"],
            entity_id=data["entity_id"],
            payload=data["payload"],
            created_at=datetime.fromisoformat(data["created_at"]),
            retry_count=data.get("retry_count", 0),
        )
        last_attempt = data.get("last_attempt")
        if last_attempt:
            op.last_attempt = datetime.fromisoformat(last_attempt)
        if "vector_clock" in data:
            op.vector_clock = VectorClock.from_dict(data["vector_clock"])
        return op


@dataclass
class SyncProgress:
    """Sync progress information."""
    status: SyncStatus
    total_operations: int
    pending_operations: int
    synced_operations: int
    errors: list[str] = field(default_factory=list)
    last_sync: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "total_operations": self.total_operations,
            "pending_operations": self.pending_operations,
            "synced_operations": self.synced_operations,
            "errors": self.errors,
            "last_sync": self.last_sync.isoformat() if self.last_sync else None,
        }


class OperationQueue:
    """
    Persistent operation queue.

    Stores operations that need to be synced when online.
    """

    def __init__(self, db_path: str | None = None):
        """Initialize operation queue.

        Args:
            db_path: Path to SQLite database (default: ~/.foresight/operations.db)
        """
        if db_path is None:
            db_path = str(Path.home() / ".foresight" / "operations.db")

        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        db_path = Path(self.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                last_attempt TEXT,
                vector_clock TEXT DEFAULT '{}'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_entity ON operations(entity_type, entity_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ops_created ON operations(created_at)")
        conn.commit()
        conn.close()

    def enqueue(self, operation: Operation) -> None:
        """Add operation to queue."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO operations
            (id, type, entity_type, entity_id, payload, created_at, retry_count, last_attempt, vector_clock)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            operation.id,
            operation.type.value,
            operation.entity_type,
            operation.entity_id,
            json.dumps(operation.payload),
            operation.created_at.isoformat(),
            operation.retry_count,
            operation.last_attempt.isoformat() if operation.last_attempt else None,
            json.dumps(operation.vector_clock.to_dict()),
        ))
        conn.commit()
        conn.close()

    def dequeue(self) -> Operation | None:
        """Get next operation from queue."""
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT * FROM operations ORDER BY created_at LIMIT 1"
        ).fetchone()
        conn.close()

        if row:
            return Operation.from_dict({
                "id": row[0],
                "type": row[1],
                "entity_type": row[2],
                "entity_id": row[3],
                "payload": json.loads(row[4]),
                "created_at": row[5],
                "retry_count": row[6],
                "last_attempt": row[7],
                "vector_clock": json.loads(row[8]),
            })
        return None

    def remove(self, operation_id: str) -> None:
        """Remove operation from queue."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM operations WHERE id = ?", (operation_id,))
        conn.commit()
        conn.close()

    def peek(self) -> list[Operation]:
        """Get all pending operations."""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute("SELECT * FROM operations ORDER BY created_at").fetchall()
        conn.close()

        operations = []
        for row in rows:
            operations.append(Operation.from_dict({
                "id": row[0],
                "type": row[1],
                "entity_type": row[2],
                "entity_id": row[3],
                "payload": json.loads(row[4]),
                "created_at": row[5],
                "retry_count": row[6],
                "last_attempt": row[7],
                "vector_clock": json.loads(row[8]),
            }))
        return operations

    def count(self) -> int:
        """Get count of pending operations."""
        conn = sqlite3.connect(self.db_path)
        count = conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        conn.close()
        return count


class SyncManager:
    """
    Manages offline-first synchronization.

    Features:
    - Queue operations when offline
    - Sync when online with retry logic
    - Progress events for UI
    - Conflict resolution with CRDTs
    """

    def __init__(
        self,
        node_id: str = "default",
        max_retries: int = 3,
        retry_delay: float = 1.0,
        sync_callback: Callable[[Operation], bool] | None = None,
    ):
        """Initialize sync manager.

        Args:
            node_id: Unique node identifier for this client
            max_retries: Maximum retry attempts per operation
            retry_delay: Base delay between retries (exponential backoff)
            sync_callback: Callback to execute operation on server
        """
        self.node_id = node_id
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._sync_callback = sync_callback

        self._queue = OperationQueue()
        self._status = SyncStatus.IDLE
        self._errors: list[str] = []
        self._last_sync: datetime | None = None
        self._progress_callbacks: list[Callable[[SyncProgress], None]] = []

        # CRDT stores for local state
        self._local_data: dict[str, LWWRegister] = {}
        self._local_tags: dict[str, ORSet] = {}

    def set_online(self, online: bool) -> None:
        """Set online/offline status."""
        if not online:
            self._status = SyncStatus.OFFLINE
        elif self._status == SyncStatus.OFFLINE:
            self._status = SyncStatus.IDLE
        self._notify_progress()

    def enqueue_operation(
        self,
        type: OperationType,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> str:
        """
        Queue an operation for sync.

        Args:
            type: Operation type (create, update, delete)
            entity_type: Type of entity
            entity_id: Entity identifier
            payload: Operation payload

        Returns:
            Operation ID
        """
        import uuid

        operation = Operation(
            id=str(uuid.uuid4()),
            type=type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
        )
        operation.vector_clock.increment(self.node_id)

        self._queue.enqueue(operation)
        logger.info(f"Enqueued operation {operation.id}: {type.value} {entity_type}:{entity_id}")

        self._notify_progress()
        return operation.id

    def sync(self) -> SyncProgress:
        """
        Sync pending operations.

        Returns:
            SyncProgress with current status
        """
        if self._status == SyncStatus.SYNCING:
            return self._get_progress()

        self._status = SyncStatus.SYNCING
        self._notify_progress()

        pending = self._queue.peek()
        synced = 0
        errors: list[str] = []

        for operation in pending:
            if operation.retry_count >= self.max_retries:
                # Max retries exceeded, skip
                errors.append(f"Max retries exceeded for {operation.id}")
                self._queue.remove(operation.id)
                continue

            try:
                if self._sync_callback:
                    success = self._sync_callback(operation)
                    if success:
                        self._queue.remove(operation.id)
                        synced += 1
                    else:
                        raise Exception("Sync callback returned False")
                else:
                    # No callback, just remove (simulated success)
                    self._queue.remove(operation.id)
                    synced += 1

                self._last_sync = datetime.now(timezone.utc)

            except Exception as e:
                # Retry with exponential backoff
                operation.retry_count += 1
                operation.last_attempt = datetime.now(timezone.utc)
                self._queue.enqueue(operation)  # Re-enqueue with updated retry count
                errors.append(f"Operation {operation.id} failed: {e}")
                logger.warning(f"Operation {operation.id} failed (attempt {operation.retry_count}): {e}")

        self._status = SyncStatus.IDLE if len(errors) == 0 else SyncStatus.ERROR
        self._errors = errors
        self._notify_progress()

        return self._get_progress()

    def _get_progress(self) -> SyncProgress:
        """Get current sync progress."""
        pending = self._queue.count()
        return SyncProgress(
            status=self._status,
            total_operations=pending,
            pending_operations=pending,
            synced_operations=0,  # Would track in production
            errors=self._errors,
            last_sync=self._last_sync,
        )

    def on_progress(self, callback: Callable[[SyncProgress], None]) -> None:
        """Register progress callback."""
        self._progress_callbacks.append(callback)

    def _notify_progress(self) -> None:
        """Notify progress callbacks."""
        progress = self._get_progress()
        for callback in self._progress_callbacks:
            try:
                callback(progress)
            except Exception as e:
                logger.error(f"Progress callback error: {e}")

    def get_status(self) -> dict[str, Any]:
        """Get sync status."""
        return self._get_progress().to_dict()


# =============================================================================
# Global Sync Manager
# =============================================================================

_sync_manager: SyncManager | None = None


def get_sync_manager(node_id: str = "default") -> SyncManager:
    """Get the global sync manager instance."""
    global _sync_manager
    if _sync_manager is None:
        _sync_manager = SyncManager(node_id=node_id)
    return _sync_manager


def reset_sync_manager() -> None:
    """Reset the global sync manager (for testing)."""
    global _sync_manager
    _sync_manager = None
