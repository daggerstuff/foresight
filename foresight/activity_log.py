"""Best-effort JSONL activity log for Foresight memory operations.

Appends ``{"ts", "tool", "session", "op", "n"}`` events to
``~/.foresight/activity/events-YYYY-MM-DD.jsonl`` — counts and identifiers
only, never memory content. Files older than 30 days are pruned on write.

Logging is strictly best-effort: any failure is swallowed so the memory
operation it observes can never fail because of the log.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastmcp.server.dependencies import get_context

from .client_attribution import foresight_home, resolve_source_tool

logger = logging.getLogger("foresight.activity_log")

RETENTION_DAYS = 30


def activity_dir() -> Path:
    return foresight_home() / "activity"


def _current_session_id() -> str | None:
    """Best-effort MCP session id for the in-flight request."""
    try:
        ctx = get_context()
        session_id = getattr(ctx, "session_id", None)
        return str(session_id) if session_id else None
    except Exception:
        return None


def _prune_old_logs(directory: Path, today: Any) -> None:
    """Delete ``events-YYYY-MM-DD.jsonl`` files older than RETENTION_DAYS."""
    cutoff = today - timedelta(days=RETENTION_DAYS)
    for path in directory.glob("events-*.jsonl"):
        try:
            stamp = datetime.strptime(path.stem.removeprefix("events-"), "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            continue
        if stamp.date() < cutoff.date():
            try:
                path.unlink()
            except OSError:
                logger.debug("activity log prune failed for %s", path, exc_info=True)


def record_activity(op: str, n: int = 1, tool: str | None = None, session: str | None = None) -> None:
    """Append one activity event. Never raises.

    Args:
        op: Operation name (e.g. ``memory.store``, ``search``, ``catch_up``).
        n: Count associated with the op (memories stored/returned/etc.).
        tool: Override for the resolved source tool.
        session: Override for the MCP session id.
    """
    try:
        resolved_tool = tool or resolve_source_tool()
        resolved_session = session if session is not None else _current_session_id()
        now = datetime.now(UTC)
        directory = activity_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"events-{now.date().isoformat()}.jsonl"
        event = {
            "ts": now.isoformat(),
            "tool": resolved_tool,
            "session": resolved_session,
            "op": op,
            "n": int(n),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
        _prune_old_logs(directory, now)
    except Exception:
        logger.debug("activity log write failed for op=%s", op, exc_info=True)
