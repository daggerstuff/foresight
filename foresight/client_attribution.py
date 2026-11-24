"""Attribution of memory operations to the calling coding tool.

``source_service`` (see :mod:`foresight.schema`) tracks *internal* writers —
foresight itself, ai-services, the Astro frontend. ``source_tool`` is the
orthogonal concept: which *coding tool* the human is driving (claude-code,
cursor, codex, gemini-cli, ...).

Resolution order — first hit wins, never guessed:

1. Explicit ``source_tool`` argument, then the ``FORESIGHT_CLIENT`` env var.
2. MCP ``clientInfo.name`` from the initialize handshake, read from the
   request-scoped fastmcp context (or pre-seeded by a middleware ContextVar).
3. Well-known environment markers (``CLAUDECODE``, ``CURSOR_*``, ``CODEX_*``,
   ``GEMINI_*``, ...). Only documented marker variables are mapped.
4. ``"unknown"``.

Also owns the small on-disk stores used by the catch-up flow:
``~/.foresight/cursors.json`` (per user/tool/project checkpoints) and the
``FORESIGHT_HOME`` override used by tests.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import Middleware as _Middleware


def _optional_module(name: str) -> Any:
    """Import a platform module, or None when this platform lacks it."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


fcntl = _optional_module("fcntl")
msvcrt = _optional_module("msvcrt")
_msvcrt_locking = getattr(msvcrt, "locking", None)
_MSVCRT_LOCK = getattr(msvcrt, "LK_LOCK", 2)
_MSVCRT_UNLOCK = getattr(msvcrt, "LK_UNLCK", 0)

logger = logging.getLogger("foresight.client_attribution")

UNKNOWN_TOOL = "unknown"

# (env var name or prefix, canonical tool name). Prefixes end with "_" or are
# matched exactly — only variables we actually know belong to a tool are
# listed; anything unrecognized falls through to "unknown".
_ENV_TOOL_MARKERS: tuple[tuple[str, str], ...] = (
    ("CLAUDECODE", "claude-code"),
    ("CLAUDE_CODE_", "claude-code"),
    ("CURSOR_", "cursor"),
    ("CODEX_CLI", "codex"),
    ("CODEX_", "codex"),
    # Only the explicit CLI marker: a bare GEMINI_ prefix also matches
    # GEMINI_API_KEY, which is a credential, not evidence of the calling tool.
    ("GEMINI_CLI", "gemini-cli"),
    ("COPILOT_", "github-copilot"),
    ("AUGMENT_", "augment"),
    ("WINDSURF_", "windsurf"),
    ("OPENCODE_", "opencode"),
)

# Seeded by AttributionMiddleware (or tests) with the handshake clientInfo.name
# for the duration of one tool call. ContextVars propagate into the anyio
# threadpool that runs sync tools, so write paths see the same value.
_request_client_name: ContextVar[str | None] = ContextVar("foresight_mcp_client_name", default=None)


def foresight_home() -> Path:
    """Return the Foresight state directory (``~/.foresight`` by default)."""
    override = os.environ.get("FORESIGHT_HOME")
    if override:
        return Path(override)
    return Path.home() / ".foresight"


def seed_request_client(name: str | None):
    """Set the request-scoped client name; returns a token for ``ContextVar.reset``."""
    return _request_client_name.set(name) if name else None


def client_name_from_fastmcp_context(ctx: Any) -> str | None:
    """Extract ``clientInfo.name`` from a fastmcp ``Context`` (best-effort)."""
    try:
        request_ctx = getattr(ctx, "request_context", None)
        if request_ctx is None:
            return None
        session = getattr(request_ctx, "session", None)
        params = getattr(session, "client_params", None) if session is not None else None
        info = getattr(params, "clientInfo", None) or getattr(params, "client_info", None)
        name = getattr(info, "name", None)
        if isinstance(name, str) and name.strip():
            return name.strip()
    except Exception:
        logger.debug("clientInfo extraction failed", exc_info=True)
    return None


def _mcp_client_name() -> str | None:
    """Resolve the MCP client name: seeded value first, live context second."""
    seeded = _request_client_name.get()
    if seeded:
        return seeded
    try:
        return client_name_from_fastmcp_context(get_context())
    except Exception:
        return None


def _env_marker_tool(environ: dict[str, str] | None = None) -> str | None:
    """Map well-known agent environment markers to a canonical tool name."""
    env = environ if environ is not None else os.environ
    for marker, tool in _ENV_TOOL_MARKERS:
        for var, value in env.items():
            if not value:
                continue
            if marker.endswith("_"):
                if var.startswith(marker):
                    return tool
            elif var == marker:
                return tool
    return None


def resolve_source_tool(explicit: str | None = None) -> str:
    """Resolve the calling coding tool for this request.

    Args:
        explicit: Optional per-call override (e.g. ``MemoryOptions.source_tool``).

    Returns:
        The tool name, or ``"unknown"`` when nothing identifies the caller.
    """
    candidates = (
        explicit,
        os.environ.get("FORESIGHT_CLIENT"),
        _request_client_name.get(),
        _mcp_client_name(),
    )
    for candidate in candidates:
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return _env_marker_tool() or UNKNOWN_TOOL


# ---------------------------------------------------------------------------
# Catch-up cursors (~/.foresight/cursors.json)
# ---------------------------------------------------------------------------

_cursors_lock = threading.Lock()


def _open_lock_file() -> Any | None:
    """Open the sibling lock file, or None when the home is not writable."""
    try:
        lock_path = _cursors_path().with_suffix(".json.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        return open(lock_path, "a+", encoding="utf-8")
    except OSError:
        logger.debug("cursor lock unavailable; using the thread lock only", exc_info=True)
        return None


def _lock_handle(handle: Any) -> bool:
    """Take an exclusive OS lock on *handle*; False when unsupported."""
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return True
    if _msvcrt_locking is not None:
        handle.seek(0)
        _msvcrt_locking(handle.fileno(), _MSVCRT_LOCK, 1)
        return True
    return False


def _unlock_handle(handle: Any, locked: bool) -> None:
    if not locked:
        return
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    elif _msvcrt_locking is not None:
        handle.seek(0)
        _msvcrt_locking(handle.fileno(), _MSVCRT_UNLOCK, 1)


@contextmanager
def _cursors_guard() -> Iterator[None]:
    """Serialize cursor read-modify-write across threads and processes.

    The threading lock covers this process; an exclusive OS lock on a sibling
    lock file covers every other server sharing ``FORESIGHT_HOME``. Lock setup
    is best-effort: a read-only home must not make catch-up fail outright.
    """
    with _cursors_lock:
        handle = _open_lock_file()
        if handle is None:
            yield
            return
        with handle:
            locked = False
            try:
                locked = _lock_handle(handle)
            except OSError:
                logger.debug("cursor OS lock failed; using the thread lock only", exc_info=True)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    _unlock_handle(handle, locked)


def _cursors_path() -> Path:
    return foresight_home() / "cursors.json"


def load_cursors() -> dict[str, str]:
    """Load the catch-up cursor map; returns ``{}`` on any read failure."""
    try:
        raw = json.loads(_cursors_path().read_text())
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
    except Exception:
        logger.debug("failed to load catch-up cursors", exc_info=True)
    return {}


def save_cursors(cursors: dict[str, str]) -> None:
    """Persist the cursor map atomically; failures are logged, never raised."""
    try:
        path = _cursors_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(cursors, indent=2, sort_keys=True))
            os.replace(tmp_name, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except Exception:
        logger.debug("failed to persist catch-up cursors", exc_info=True)


def read_cursor(key: str) -> str | None:
    """Return one cursor value under the cross-process lock."""
    with _cursors_guard():
        return load_cursors().get(key)


def _is_newer(candidate: str, current: str | None) -> bool:
    """True when *candidate* is later than *current*.

    A cursor is ``<timestamp>#<id>``, so only the timestamp half is parsed (it
    may carry a different UTC offset); the id half breaks ties. Values that do
    not parse fall back to a lexical comparison.
    """
    if current is None:
        return True
    candidate_time, _, candidate_id = candidate.partition("#")
    current_time, _, current_id = current.partition("#")
    try:
        candidate_dt = datetime.fromisoformat(candidate_time)
        current_dt = datetime.fromisoformat(current_time)
        if candidate_dt != current_dt:
            return candidate_dt > current_dt
        return candidate_id > current_id
    except (TypeError, ValueError):
        return candidate > current


def update_cursor(key: str, timestamp: str) -> None:
    """Advance a single cursor under the cross-process lock.

    Cursors only move forward: an overlapping catch-up that queried an older
    window cannot rewind a checkpoint another call already advanced, which
    would make later catch-ups replay the same memories.
    """
    with _cursors_guard():
        cursors = load_cursors()
        if not _is_newer(timestamp, cursors.get(key)):
            return
        cursors[key] = timestamp
        save_cursors(cursors)


# ---------------------------------------------------------------------------
# FastMCP middleware — seed the handshake client name per tool call
# ---------------------------------------------------------------------------


class AttributionMiddleware(_Middleware):
    """Seeds the request-scoped MCP client name for tool calls.

    Reads ``clientInfo.name`` from the initialize handshake via the fastmcp
    context and stores it in :data:`_request_client_name` so write paths that
    run outside a live fastmcp context (capture pipeline, curation) still see
    the calling tool.
    """

    async def on_call_tool(self, context: Any, call_next: Any) -> Any:
        name = client_name_from_fastmcp_context(getattr(context, "fastmcp_context", None))
        token = seed_request_client(name)
        try:
            return await call_next(context)
        finally:
            if token is not None:
                _request_client_name.reset(token)
