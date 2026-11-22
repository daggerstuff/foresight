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

import json
import logging
import os
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from fastmcp.server.dependencies import get_context
from fastmcp.server.middleware import Middleware as _Middleware

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
    ("GEMINI_CLI", "gemini-cli"),
    ("GEMINI_", "gemini-cli"),
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
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cursors, indent=2, sort_keys=True))
        tmp.replace(path)
    except Exception:
        logger.debug("failed to persist catch-up cursors", exc_info=True)


def update_cursor(key: str, timestamp: str) -> None:
    """Advance a single cursor under the module lock."""
    with _cursors_lock:
        cursors = load_cursors()
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
