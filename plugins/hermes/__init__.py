"""Foresight memory provider — persistent shared brain via the Foresight REST API.

Plugs the shared Foresight brain (Postgres/pgvector behind
``foresight.pixelated.love`` or a local ``foresight.service``) into Hermes
Agent as a first-class memory provider:
- Hands-off proactive recall: synchronous, relevance-ranked context surfaced
  each turn with deterministic UI recall indicator (🔮 Foresight).
- Hands-off continuous distillation: background turn sync, pre-compression
  checkpoints, end-of-session distillation, and subagent delegation memory.
- Seamless built-in mirroring: memory-tool writes (add/replace/remove) reflected
  into the shared fleet-wide brain.
- Rich toolset: search (hybrid/semantic/keyword), remember, reflect (multi-hop
  reasoning synthesis), profile (user preferences & mental models), context,
  list, and forget.
- CLI & status integration: ``hermes memory status`` and ``hermes foresight ...``
  subcommands.

Config (env vars, profile-scoped via secret scope):
  FORESIGHT_URL       base URL, default http://127.0.0.1:8764
  FORESIGHT_API_KEY   optional Bearer key (required when the server sets
                      FORESIGHT_REQUIRE_API_KEY=1)
  FORESIGHT_USER_ID   optional X-User-Id identity lane override
"""

from __future__ import annotations

import http.client
import importlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Match trivial prompts with no semantic signal (greetings, confirmations, commands)
_TRIVIAL_PROMPT_RE = re.compile(
    r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"hi|hey|hello|yo|sup|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)"
    r"[\s!?.:;,'" + r"~\u2018\u2019\u201c\u201d\u2014\u2013\u2026()\[\]{}<>*&^%$#@!+=`\u00a0]*$",
    re.IGNORECASE,
)

# Graceful Hermes agent imports with testing/standalone fallbacks
try:
    from agent.memory_provider import (
        INDICATOR_GLYPH,
        MemoryProvider,
        RecallStatus,
        is_trivial_prompt,
        spawn_context_thread,
    )
except ImportError:
    INDICATOR_GLYPH = "🧠"

    class RecallStatus:  # type: ignore[no-redef]
        def __init__(self, provider_label: str, count: int, glyph: str = INDICATOR_GLYPH) -> None:
            self.provider_label = provider_label
            self.count = count
            self.glyph = glyph

        def __repr__(self) -> str:
            return f"RecallStatus({self.provider_label!r}, count={self.count}, glyph={self.glyph!r})"

    class MemoryProvider:  # type: ignore[no-redef]
        pass

    def spawn_context_thread(  # type: ignore[no-redef]
        target: Callable[..., Any],
        *,
        name: str,
        daemon: bool = True,
        args: tuple = (),
        kwargs: dict[str, Any] | None = None,
    ) -> threading.Thread:
        return threading.Thread(target=target, name=name, daemon=daemon, args=args, kwargs=kwargs or {})

    def is_trivial_prompt(text: str | None) -> bool:  # type: ignore[no-redef]
        stripped = (text or "").strip()
        if not stripped or stripped.startswith("/"):
            return True
        return bool(_TRIVIAL_PROMPT_RE.match(stripped))


try:
    from agent.secret_scope import get_secret
except ImportError:

    def get_secret(key: str, default: str = "") -> str:  # type: ignore[misc]
        return os.environ.get(key, default)


try:
    from tools.registry import tool_error
except ImportError:

    def tool_error(message: str) -> str:  # type: ignore[misc]
        return json.dumps({"error": message})


logger = logging.getLogger(__name__)

# Local transcript buffer bounds for distillation.
TRANSCRIPT_BUFFER_LIMIT = 100
TRANSCRIPT_BUFFER_KEEP = 60

# Hermes pre-compress checkpoint contract version this provider implements.
PRE_COMPRESS_CHECKPOINT_API_VERSION = 2

_DEFAULT_URL = "http://127.0.0.1:8764"
_DEFAULT_GLYPH = "🔮"
_USER_AGENT = "hermes-foresight/2.0"
_MASKED_FIELD = True
_HTTP_CLIENT_ERROR = 400


def _load_config(hermes_home: Path | None) -> dict[str, Any]:
    """Non-secret ``memory.foresight`` values from the active profile's config.yaml."""
    if not hermes_home:
        return {}
    cfg_path = Path(hermes_home) / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        yaml = importlib.import_module("yaml")

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        section = data.get("memory", {}).get("foresight", {})
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


def _sanitize_user_id(raw_uid: str) -> str:
    """Sanitize user ID to Foresight's [A-Za-z0-9_-]{1,64} requirement."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", str(raw_uid or "")).strip("_")
    return cleaned[:64] if cleaned else "default"


def _detect_git_root(start_path: Path | str | None = None) -> str | None:
    """Best-effort git repository root detection for project context."""
    cur = Path(start_path).resolve() if start_path else Path.cwd().resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists():
            return str(parent)
    return str(cur) if start_path else None


def _send(
    method: str,
    url: str,
    data: bytes | None,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, str, bytes]:
    """Issue one request against the configured endpoint and return status, reason, body."""
    parts = urllib.parse.urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    connection_type = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parts.hostname or "", parts.port, timeout=timeout)
    try:
        connection.request(method, target, body=data, headers=headers)
        response = connection.getresponse()
        return response.status, str(response.reason), response.read()
    finally:
        connection.close()


@dataclass(frozen=True)
class MemoryWrite:
    """The optional fields of one ``POST /memories`` body."""

    category: str = "fact"
    importance: float = 0.5
    tags: list[str] | None = None
    scope: str | None = None
    retention: str | None = None


class _Client:
    """Robust REST client for the Foresight HTTP API."""

    def __init__(self, base_url: str, api_key: str, user_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        params: dict | None = None,
        timeout: float = 8.0,
    ) -> Any:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.user_id:
            headers["X-User-Id"] = self.user_id

        url = f"{self.base_url}{path}"
        if params:
            query_str = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if query_str:
                url = f"{url}?{query_str}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            status, reason, raw = _send(method, url, data, headers, timeout)
        except OSError as exc:
            logger.debug("Foresight connection error (%s %s): %s", method, path, exc)
            raise RuntimeError(f"Connection failed: {exc}") from exc
        if status >= _HTTP_CLIENT_ERROR:
            err_body = ""
            with suppress(Exception):
                err_body = raw.decode("utf-8")
                parsed = json.loads(err_body)
                if isinstance(parsed, dict) and "detail" in parsed:
                    err_body = parsed["detail"]
                elif isinstance(parsed, dict) and "error" in parsed:
                    err_body = str(parsed["error"])
            msg = f"HTTP {status}: {err_body or reason}"
            logger.debug("Foresight HTTP error (%s %s): %s", method, path, msg)
            raise RuntimeError(msg)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def health(self) -> dict[str, Any]:
        """Check server connectivity and latency."""
        t0 = time.monotonic()
        try:
            res = self.request("GET", "/memories", params={"limit": 1}, timeout=3.5)
            latency = int((time.monotonic() - t0) * 1000)
            count = None
            if isinstance(res, dict) and "result" in res and isinstance(res["result"], str):
                m = re.search(r"\((\d+)\s+found\)", res["result"])
                if m:
                    count = int(m.group(1))
            return {"ok": True, "latency_ms": latency, "count": count}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "latency_ms": int((time.monotonic() - t0) * 1000)}

    # -- Endpoints -------------------------------------------------------------

    def inject(
        self,
        query: str,
        max_memories: int = 5,
        max_chars: int = 4000,
        include_details: bool = True,
        project: str | None = None,
    ) -> dict[str, Any]:
        """Surface relevant memories and context blocks."""
        body: dict[str, Any] = {
            "conversation_text": query,
            "max_memories": max_memories,
            "max_chars": max_chars,
            "include_details": include_details,
        }
        if project:
            body["project"] = project
        out = self.request("POST", "/inject", body=body, timeout=6.0)
        return out if isinstance(out, dict) else {"formatted": str(out) if out else ""}

    def search(
        self,
        query: str,
        limit: int = 8,
        query_type: str = "keyword",
        use_hybrid: bool = True,
        category: str | None = None,
    ) -> Any:
        """Search memories via hybrid/semantic/keyword query."""
        payload: dict[str, Any] = {
            "query_type": query_type,
            "query": query,
            "limit": limit,
            "use_hybrid": use_hybrid,
        }
        if category:
            payload["category"] = category
        return self.request("POST", "/search", body=payload, timeout=8.0)

    def store(self, content: str, write: MemoryWrite | None = None) -> Any:
        """Store a durable memory."""
        write = write or MemoryWrite()
        body: dict[str, Any] = {
            "content": content,
            "category": write.category,
            "importance": write.importance,
            "tags": write.tags or [],
        }
        if write.scope:
            body["scope"] = write.scope
        if write.retention:
            body["retention"] = write.retention
        return self.request("POST", "/memories", body=body, timeout=8.0)

    def reflect(self, question: str) -> Any:
        """Synthesize multi-hop reasoning across disparate memories."""
        return self.request("POST", "/reflect", body={"question": question}, timeout=15.0)

    def profile(self, format_type: str = "json") -> Any:
        """Fetch synthesized user profile, preferences, and mental models."""
        return self.request("GET", "/profile", params={"format": format_type}, timeout=8.0)

    def forget(self, memory_id: str) -> Any:
        """Delete a memory by ID."""
        return self.request("DELETE", f"/memories/{urllib.parse.quote(memory_id, safe='')}", timeout=6.0)

    def list_memories(self, limit: int = 20) -> Any:
        """List recent memories."""
        return self.request("GET", "/memories", params={"limit": limit}, timeout=8.0)

    def transcript(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        project_path: str | None = None,
        user_id: str | None = None,
        infer: bool = True,
    ) -> Any:
        """Distill session transcript into persistent memory."""
        body: dict[str, Any] = {
            "session_id": session_id,
            "messages": messages,
            "infer": infer,
        }
        if project_path:
            body["project_path"] = project_path
        if user_id:
            body["user_id"] = user_id
        return self.request("POST", "/session/transcript", body=body, timeout=30.0)


def _quiet(label: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        logger.debug("Foresight %s failed: %s", label, exc)
        return None


def _schema(name: str, description: str, properties: dict | None = None, required: tuple = ()) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": list(required)},
    }


def _p(description: str, type_: str = "string", **extra) -> dict[str, Any]:
    return {"type": type_, "description": description, **extra}


_SCHEMAS = [
    _schema(
        "foresight_search",
        "Search the shared Foresight memory brain for relevant memories using hybrid (vector + keyword) search.",
        {
            "query": _p("Search query string"),
            "limit": _p("Max results (default: 8)", "integer"),
            "category": _p("Optional category filter (e.g. fact, preference, decision)"),
        },
        ("query",),
    ),
    _schema(
        "foresight_remember",
        "Store a durable fact, preference, architecture decision, or pattern in the shared Foresight brain.",
        {
            "content": _p("Memory content to store"),
            "category": _p("Category (fact, preference, decision, directive, pattern)"),
            "importance": _p("Importance score from 0.0 to 1.0", "number"),
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional categorization tags"},
        },
        ("content",),
    ),
    _schema(
        "foresight_reflect",
        "Synthesize reasoning and insights across stored memories to answer a strategic or background question.",
        {"question": _p("Question or topic requiring synthesized memory reflection")},
        ("question",),
    ),
    _schema(
        "foresight_profile",
        "Retrieve the user's synthesized profile — preferences, persistent traits, and active mental models.",
        {},
    ),
    _schema(
        "foresight_context",
        "Retrieve injected Foresight context (memories + standing guidance) for a topic.",
        {"query": _p("Topic or conversation text to inject context for")},
        ("query",),
    ),
    _schema(
        "foresight_forget",
        "Delete a specific memory from the shared Foresight brain by ID.",
        {"memory_id": _p("Memory ID to delete")},
        ("memory_id",),
    ),
    _schema(
        "foresight_list",
        "List recent memories from the shared Foresight brain.",
        {"limit": _p("Max results (default: 20)", "integer")},
    ),
]


class ForesightMemoryProvider(MemoryProvider):
    """Foresight persistent memory provider — hands-off recall, proactive context injection,

    transcript distillation, and rich tools over the shared fleet-wide brain.
    """

    # Opt in to fail-closed API v2 pre-compression checkpoint contract (#140058)
    pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION

    def __init__(self) -> None:
        self._client: _Client | None = None
        self._session_id = ""
        self._project_path: str | None = None
        self._hermes_home: Path | None = None
        self._lock = threading.Lock()
        self._last_recall_status: RecallStatus | None = None
        self._last_prefetch_cache = ""
        self._last_prefetch_query = ""
        self._sync_thread: threading.Thread | None = None
        self._memwrite_thread: threading.Thread | None = None
        self._session_transcripts: dict[str, list[dict[str, str]]] = {}
        self._auto_distill = True
        self._max_memories = 5

    @property
    def name(self) -> str:
        return "foresight"

    def is_available(self) -> bool:
        """Fast check if this provider can activate. No blocking network calls."""
        return True

    def unavailable_reason(self) -> str:
        return ""

    def get_config_schema(self) -> list[dict[str, Any]]:
        """Declare interactive setup fields for ``hermes memory setup``."""
        return [
            {
                "key": "url",
                "description": "Foresight base URL (e.g. https://foresight.pixelated.love or http://127.0.0.1:8764)",
                "default": _DEFAULT_URL,
                "env_var": "FORESIGHT_URL",
            },
            {
                "key": "api_key",
                "description": "Foresight API key (Bearer token; optional on localhost)",
                "secret": _MASKED_FIELD,
                "env_var": "FORESIGHT_API_KEY",
            },
            {
                "key": "user_id",
                "description": "Memory identity lane (X-User-Id); defaults to current user profile",
                "env_var": "FORESIGHT_USER_ID",
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret fields into config.yaml under memory.foresight."""
        if not hermes_home or not values:
            return
        try:
            save_config = importlib.import_module("hermes_cli.config").save_config

            non_secret = {k: v for k, v in values.items() if k not in ("api_key",)}
            if non_secret:
                save_config({"memory": {"foresight": non_secret}}, merge_existing=True)
        except Exception as exc:
            logger.debug("Foresight save_config failed: %s", exc)

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize provider instance with profile-scoped bindings."""
        hermes_home = kwargs.get("hermes_home")
        self._hermes_home = Path(hermes_home) if hermes_home else None
        cfg = _load_config(self._hermes_home)

        base_url = (
            get_secret("FORESIGHT_URL", "")
            or str(cfg.get("url") or "")
            or os.environ.get("FORESIGHT_URL", "")
            or _DEFAULT_URL
        ).rstrip("/")
        api_key = get_secret("FORESIGHT_API_KEY", "") or os.environ.get("FORESIGHT_API_KEY", "")

        # Explicit user configuration (env or scoped secret) is authoritative for the shared brain.
        # Fall back to caller / profile identity from kwargs (sanitized to Foresight's [A-Za-z0-9_-] rule).
        configured_uid = (
            get_secret("FORESIGHT_USER_ID", "")
            or os.environ.get("FORESIGHT_USER_ID", "")
            or os.environ.get("FORESIGHT_IDENTITY", "")
            or str(cfg.get("user_id") or "")
        )
        if configured_uid:
            user_id = _sanitize_user_id(configured_uid)
        else:
            caller_uid = str(kwargs.get("user_id") or "") or str(kwargs.get("agent_identity") or "")
            user_id = _sanitize_user_id(caller_uid)

        cwd = kwargs.get("cwd")
        self._project_path = str(cwd) if cwd else _detect_git_root()
        self._session_id = session_id or "hermes"
        self._max_memories = int(cfg.get("max_memories", 5))
        self._auto_distill = bool(cfg.get("auto_distill", True))

        self._client = _Client(base_url, api_key, user_id)
        logger.debug(
            "Foresight memory provider initialized: url=%s, user_id=%s, project=%s",
            base_url,
            user_id or "(default)",
            self._project_path,
        )

    def system_prompt_block(self) -> str:
        """Static system prompt informing the model that Foresight memory is active."""
        url = self._client.base_url if self._client else _DEFAULT_URL
        return (
            f"# Foresight Memory (Active)\n"
            f"Connected to shared memory brain at {url}.\n"
            "- Relevant past memories, project context, standing directives, and user preferences "
            "are proactively surfaced into conversation context each turn.\n"
            "- To search, store, reflect, or review memory explicitly, use the foresight tools: "
            "foresight_search, foresight_remember, foresight_reflect, foresight_profile, "
            "foresight_context, foresight_forget, foresight_list.\n"
            "- When learning key decisions, user rules, or architecture patterns, use "
            "foresight_remember to persist them for future sessions."
        )

    # -- Recall ----------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Synchronously recall relevant memories for the upcoming turn.

        Called by Hermes before the model call. Bounded by Hermes' external
        prefetch timeout (8.0s). Injects relevant memories + context blocks,
        and sets recall_status so Hermes emits the deterministic UI recall indicator.
        """
        logger.debug("Foresight prefetch for session=%r", session_id)
        if not self._client or not query or is_trivial_prompt(query):
            with self._lock:
                self._last_recall_status = None
                self._last_prefetch_cache = ""
                self._last_prefetch_query = ""
            return ""

        clean_query = query.strip()

        # Check if we already have an identical cached result
        with self._lock:
            if self._last_prefetch_query == clean_query and self._last_prefetch_cache:
                return self._last_prefetch_cache

        try:
            out = self._client.inject(
                query=clean_query,
                max_memories=self._max_memories,
                include_details=True,
                project=self._project_path,
            )
        except Exception as exc:
            logger.debug("Foresight prefetch recall failed: %s", exc)
            with self._lock:
                self._last_recall_status = None
                self._last_prefetch_cache = ""
                self._last_prefetch_query = ""
            return ""

        formatted = ""
        memories = []
        if isinstance(out, dict):
            formatted = str(out.get("formatted") or out.get("context") or out.get("text") or "").strip()
            memories = out.get("memories") or []

        if not formatted:
            with self._lock:
                self._last_recall_status = None
                self._last_prefetch_cache = ""
                self._last_prefetch_query = ""
            return ""

        # Determine count of discrete memories surfaced
        count = len(memories) if isinstance(memories, list) and memories else 0
        if count == 0:
            m = re.search(r"(\d+)\s+memories\s+surfaced", formatted)
            if m:
                count = int(m.group(1))

        injected_block = f"## Foresight Memory\n{formatted}"

        with self._lock:
            self._last_recall_status = RecallStatus(
                provider_label="Foresight",
                count=count,
                glyph=_DEFAULT_GLYPH,
            )
            self._last_prefetch_cache = injected_block
            self._last_prefetch_query = clean_query

        return injected_block

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Background pre-warm for subsequent turns (non-blocking)."""

    def recall_status(self) -> RecallStatus | None:
        """Return status of the most recent prefetch for deterministic UI indicators."""
        with self._lock:
            return self._last_recall_status

    # -- Distillation & Lifecycle Hooks ---------------------------------------

    def _sanitize_messages(self, raw_messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Normalize messages to clean {role, content} dicts."""
        clean = []
        for m in raw_messages:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role", "")).lower()
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                clean.append({"role": role, "content": content.strip()})
        return clean

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
        **_kwargs,
    ) -> None:
        """Distill the completed conversation turn into memory (background, non-blocking)."""
        if not self._client or not self._auto_distill or not user_content:
            return

        sid = session_id or self._session_id or "hermes"

        # Buffer turn locally
        with self._lock:
            transcript = self._session_transcripts.setdefault(sid, [])
            transcript.append({"role": "user", "content": user_content})
            transcript.append({"role": "assistant", "content": assistant_content})
            # Bound local transcript buffer
            if len(transcript) > TRANSCRIPT_BUFFER_LIMIT:
                self._session_transcripts[sid] = transcript[-TRANSCRIPT_BUFFER_KEEP:]

        # Prepare messages for distillation
        if messages:
            clean_messages = self._sanitize_messages(messages[-30:])
        else:
            clean_messages = list(self._session_transcripts[sid][-30:])

        if not clean_messages:
            return

        def _work() -> None:
            try:
                self._client.transcript(
                    session_id=sid,
                    messages=clean_messages,
                    project_path=self._project_path,
                    user_id=self._client.user_id,
                    infer=True,
                )
                logger.debug("Foresight background turn distillation complete for session=%s", sid)
            except Exception as exc:
                logger.debug("Foresight turn distillation failed: %s", exc)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=2.0)

        self._sync_thread = spawn_context_thread(_work, name="foresight-sync-turn")
        self._sync_thread.start()

    def on_pre_compress(
        self,
        messages: list[dict[str, Any]],
        *,
        require_checkpoint: bool = False,
    ) -> str:
        """Checkpoint and distill transcript before Hermes compresses history."""
        if not self._client or not messages:
            return ""

        clean = self._sanitize_messages(messages)
        if not clean:
            return ""

        sid = self._session_id or "compress"
        try:
            self._client.transcript(
                session_id=sid,
                messages=clean,
                project_path=self._project_path,
                user_id=self._client.user_id,
                infer=True,
            )
            msg = f"[Foresight Checkpoint] Distilled {len(clean)} messages into persistent brain."
            logger.info(msg)
            return msg
        except Exception as exc:
            logger.warning("Foresight on_pre_compress failed: %s", exc)
            if require_checkpoint:
                raise
            return ""

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """End-of-session comprehensive transcript extraction."""
        if not self._client or not messages:
            return

        clean = self._sanitize_messages(messages)
        if not clean:
            return

        sid = self._session_id or "session_end"

        def _work() -> None:
            try:
                self._client.transcript(
                    session_id=sid,
                    messages=clean,
                    project_path=self._project_path,
                    user_id=self._client.user_id,
                    infer=True,
                )
                logger.debug("Foresight session-end distillation finished for session=%s", sid)
            except Exception as exc:
                logger.debug("Foresight on_session_end distillation failed: %s", exc)

        spawn_context_thread(_work, name="foresight-session-end").start()

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **_kwargs,
    ) -> None:
        """Handle session reassignment mid-process."""
        logger.debug(
            "Foresight session switch to %s (parent=%r, rewound=%s)",
            new_session_id,
            parent_session_id,
            rewound,
        )
        self._session_id = new_session_id
        if reset:
            with self._lock:
                self._session_transcripts.pop(new_session_id, None)

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **_kwargs,
    ) -> None:
        """Persist findings and decisions from completed subagent delegations."""
        logger.debug("Foresight delegation recorded for session=%r", child_session_id)
        if not self._client or not task or not result:
            return

        summary = f"Subagent Delegation Result:\nTask: {task[:500]}\nOutcome: {result[:1200]}"

        def _work() -> None:
            try:
                self._client.store(
                    summary,
                    MemoryWrite(
                        category="decision",
                        importance=0.6,
                        tags=["delegation", "subagent"],
                    ),
                )
            except Exception as exc:
                logger.debug("Foresight on_delegation failed: %s", exc)

        spawn_context_thread(_work, name="foresight-delegation").start()

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Mirror built-in memory-tool writes (add/replace/remove) to Foresight."""
        logger.debug("Foresight mirroring built-in memory %s on %s (metadata=%r)", action, target, metadata)
        if not self._client or not content:
            return

        category = "preference" if target == "user" else "fact"
        tags = ["built-in-mirror", f"target:{target}"]

        def _work() -> None:
            try:
                if action == "add":
                    self._client.store(content, MemoryWrite(category=category, importance=0.7, tags=tags))
                elif action == "replace":
                    self._client.store(
                        content,
                        MemoryWrite(category=category, importance=0.7, tags=[*tags, "replaced"]),
                    )
                elif action == "remove":
                    self._client.store(
                        f"[Deleted Memory] {content}",
                        MemoryWrite(
                            category="archived",
                            importance=0.1,
                            tags=[*tags, "deleted"],
                            retention="ephemeral",
                        ),
                    )
            except Exception as exc:
                logger.debug("Foresight on_memory_write failed: %s", exc)

        t = spawn_context_thread(_work, name="foresight-memwrite")
        self._memwrite_thread = t
        t.start()

    def get_status_config(self, provider_config: dict[str, Any]) -> dict[str, Any]:
        """Status details displayed by ``hermes memory status``."""
        url = (
            (self._client.base_url if self._client else "")
            or get_secret("FORESIGHT_URL", "")
            or str(provider_config.get("url") or "")
            or os.environ.get("FORESIGHT_URL", "")
            or _DEFAULT_URL
        ).rstrip("/")

        configured_uid = (
            get_secret("FORESIGHT_USER_ID", "")
            or os.environ.get("FORESIGHT_USER_ID", "")
            or os.environ.get("FORESIGHT_IDENTITY", "")
            or str(provider_config.get("user_id") or "")
        )
        raw_uid = (self._client.user_id if self._client else "") or configured_uid or "default"
        user_id = _sanitize_user_id(raw_uid)

        api_key = (
            (self._client.api_key if self._client else "")
            or get_secret("FORESIGHT_API_KEY", "")
            or os.environ.get("FORESIGHT_API_KEY", "")
        )

        status_info: dict[str, Any] = {
            "url": url,
            "identity_lane": user_id or "(server default)",
            "mode": "hands-off proactive recall & auto-distillation ✓",
        }

        probe_client = self._client or _Client(url, api_key, user_id)
        try:
            health = probe_client.health()
            if health.get("ok"):
                status_info["connection"] = f"connected ✓ ({health.get('latency_ms')}ms)"
                if health.get("count") is not None:
                    status_info["memories_in_brain"] = health["count"]
            else:
                status_info["connection"] = f"degraded / unreachable ({health.get('error')})"
        except Exception as exc:
            status_info["connection"] = f"unreachable ({exc})"

        return status_info

    # -- Tools -----------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return list(_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **_kwargs) -> str:
        if not self._client:
            return tool_error("Foresight provider not initialized")
        try:
            return json.dumps(self._dispatch(tool_name, args), indent=2)
        except Exception as exc:
            return tool_error(str(exc))

    def _dispatch(self, tool_name: str, args: dict[str, Any]) -> Any:
        tools: dict[str, Callable[[dict[str, Any]], Any]] = {
            "foresight_search": self._tool_search,
            "foresight_remember": self._tool_remember,
            "foresight_reflect": self._tool_reflect,
            "foresight_profile": self._tool_profile,
            "foresight_context": self._tool_context,
            "foresight_forget": self._tool_forget,
            "foresight_list": self._tool_list,
        }
        tool = tools.get(tool_name)
        if tool is None:
            return {"error": f"Unknown tool: {tool_name}"}
        return tool(args)

    def _tool_search(self, args: dict[str, Any]) -> Any:
        return self._client.search(
            query=args.get("query", ""),
            limit=min(int(args.get("limit", 8)), 25),
            category=args.get("category"),
            use_hybrid=True,
        )

    def _tool_remember(self, args: dict[str, Any]) -> Any:
        importance = args.get("importance")
        return self._client.store(
            args.get("content", ""),
            MemoryWrite(
                category=args.get("category") or "fact",
                importance=float(importance) if importance is not None else 0.5,
                tags=args.get("tags") or [],
            ),
        )

    def _tool_reflect(self, args: dict[str, Any]) -> Any:
        return self._client.reflect(args.get("question", ""))

    def _tool_profile(self, _args: dict[str, Any]) -> Any:
        return self._client.profile()

    def _tool_context(self, args: dict[str, Any]) -> Any:
        return self._client.inject(
            query=args.get("query", ""),
            max_memories=self._max_memories,
            include_details=False,
            project=self._project_path,
        )

    def _tool_forget(self, args: dict[str, Any]) -> Any:
        return self._client.forget(args.get("memory_id", ""))

    def _tool_list(self, args: dict[str, Any]) -> Any:
        return self._client.list_memories(limit=min(int(args.get("limit", 20)), 50))

    def shutdown(self) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=3.0)
        if self._memwrite_thread and self._memwrite_thread.is_alive():
            self._memwrite_thread.join(timeout=3.0)
        self._client = None


def register(ctx) -> None:
    """Register Foresight as a first-class memory provider plugin."""
    ctx.register_memory_provider(ForesightMemoryProvider())
