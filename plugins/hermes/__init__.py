"""Foresight memory provider — persistent memory via the Foresight REST API.

Plugs the shared Foresight brain (Postgres/pgvector behind
``foresight.pixelated.love`` or a local ``foresight.service``) into Hermes
Agent as a first-class memory provider: prefetch recall each turn, transcript
distillation on turn/session end, and ``foresight_*`` tools for explicit
search/store/forget.

Config (env vars, profile-scoped via the secret scope):
  FORESIGHT_URL       base URL, default http://127.0.0.1:8764
  FORESIGHT_API_KEY   optional Bearer key (required when the server sets
                      FORESIGHT_REQUIRE_API_KEY=1)
  FORESIGHT_USER_ID   optional X-User-Id identity lane override
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from agent.memory_provider import MemoryProvider
from agent.secret_scope import get_secret
from tools.registry import tool_error

logger = logging.getLogger(__name__)

_DEFAULT_URL = "http://127.0.0.1:8764"


def _load_config(hermes_home: Path | None) -> dict[str, Any]:
    """Non-secret ``memory.foresight`` values from the profile's config.yaml."""
    if not hermes_home:
        return {}
    cfg_path = Path(hermes_home) / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        section = data.get("memory", {}).get("foresight", {})
        return section if isinstance(section, dict) else {}
    except Exception:
        return {}


class _Client:
    """Minimal REST client for the Foresight HTTP API."""

    def __init__(self, base_url: str, api_key: str, user_id: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id

    def request(self, method: str, path: str, body: dict | None = None, timeout: float = 8.0) -> Any:
        headers = {
            "Content-Type": "application/json",
            # Cloudflare bot rules (error 1010) ban the default Python-urllib UA.
            "User-Agent": "hermes-foresight/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.user_id:
            headers["X-User-Id"] = self.user_id
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        return json.loads(raw) if raw else {}

    # -- Foresight endpoints -------------------------------------------------

    def inject(self, query: str, max_memories: int = 5, max_chars: int = 4000) -> str:
        out = self.request(
            "POST",
            "/inject",
            {"conversation_text": query, "max_memories": max_memories, "max_chars": max_chars},
        )
        if isinstance(out, dict):
            for key in ("formatted", "context", "text", "payload"):
                if isinstance(out.get(key), str) and out[key]:
                    return out[key]
        return ""

    def store(self, content: str, category: str = "fact", importance: float = 0.5, tags: list | None = None) -> Any:
        return self.request(
            "POST",
            "/memories",
            {"content": content, "category": category, "importance": importance, "tags": tags or []},
        )

    def search(self, query: str, limit: int = 8) -> Any:
        return self.request("POST", "/search", {"query_type": "keyword", "query": query, "limit": limit})

    def forget(self, memory_id: str) -> Any:
        return self.request("DELETE", f"/memories/{memory_id}")

    def list_memories(self, limit: int = 20) -> Any:
        return self.request("GET", f"/memories?limit={limit}")

    def transcript(self, session_id: str, messages: list[dict]) -> Any:
        return self.request(
            "POST",
            "/session/transcript",
            {"session_id": session_id, "messages": messages},
            timeout=20.0,
        )


def _quiet(label: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        logger.debug("Foresight %s failed: %s", label, exc)
        return None


def _schema(name: str, description: str, properties: dict | None = None, required: tuple = ()) -> dict:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": list(required)},
    }


def _p(description: str, type_: str = "string", **extra) -> dict:
    return {"type": type_, "description": description, **extra}


_SCHEMAS = [
    _schema(
        "foresight_search",
        "Search the shared Foresight memory brain for relevant memories.",
        {"query": _p("Search query"), "limit": _p("Max results", "integer")},
        ("query",),
    ),
    _schema(
        "foresight_remember",
        "Store a durable fact, preference, or decision in the shared Foresight memory brain.",
        {
            "content": _p("Memory content to store"),
            "category": _p("Category (fact, preference, decision, ...)"),
            "importance": _p("Importance 0.0-1.0", "number"),
        },
        ("content",),
    ),
    _schema(
        "foresight_context",
        "Retrieve injected Foresight context (memories + standing guidance) for a topic.",
        {"query": _p("Topic or conversation text to inject context for")},
        ("query",),
    ),
    _schema(
        "foresight_forget",
        "Delete a memory from the shared Foresight brain by ID.",
        {"memory_id": _p("Memory ID to delete")},
        ("memory_id",),
    ),
    _schema(
        "foresight_list",
        "List recent memories from the shared Foresight brain.",
        {"limit": _p("Max results", "integer")},
    ),
]


class ForesightMemoryProvider(MemoryProvider):
    """Foresight persistent memory — recall, transcript capture, and tools over REST."""

    def __init__(self) -> None:
        self._client: _Client | None = None
        self._session_id = ""
        self._lock = threading.Lock()
        self._prefetch_result = ""
        self._prefetch_thread: threading.Thread | None = None

    @property
    def name(self) -> str:
        return "foresight"

    def is_available(self) -> bool:
        # URL defaults to the co-located service; an explicit env/config value
        # (including a remote endpoint) also counts as configured.
        return True

    def unavailable_reason(self) -> str:
        return ""

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "url",
                "description": "Foresight base URL",
                "default": _DEFAULT_URL,
                "env_var": "FORESIGHT_URL",
            },
            {
                "key": "api_key",
                "description": "Foresight API key (Bearer token; optional on localhost)",
                "secret": True,
                "env_var": "FORESIGHT_API_KEY",
            },
            {
                "key": "user_id",
                "description": "Memory identity lane (X-User-Id); defaults to the server's configured user",
                "env_var": "FORESIGHT_USER_ID",
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        """All fields carry ``env_var`` — configuration lives in the environment."""

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = kwargs.get("hermes_home")
        cfg = _load_config(Path(hermes_home) if hermes_home else None)
        base_url = (
            get_secret("FORESIGHT_URL", "")
            or str(cfg.get("url") or "")
            or os.environ.get("FORESIGHT_URL", "")
            or _DEFAULT_URL
        ).rstrip("/")
        api_key = get_secret("FORESIGHT_API_KEY", "") or os.environ.get("FORESIGHT_API_KEY", "")
        user_id = (
            get_secret("FORESIGHT_USER_ID", "")
            or os.environ.get("FORESIGHT_USER_ID", "")
            or os.environ.get("FORESIGHT_IDENTITY", "")
            or str(kwargs.get("user_id") or "")
        )
        self._client = _Client(base_url, api_key, user_id)
        self._session_id = session_id

    def system_prompt_block(self) -> str:
        url = self._client.base_url if self._client else _DEFAULT_URL
        return (
            f"# Foresight Memory\nActive — shared brain at {url}.\n"
            "Relevant memories are auto-injected each turn; use foresight_search / "
            "foresight_remember / foresight_context / foresight_forget / foresight_list "
            "for explicit recall and storage."
        )

    # -- Recall ---------------------------------------------------------------

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire a background /inject recall; ``prefetch`` consumes it next turn."""
        if not self._client or not query:
            return
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            return

        def _run() -> None:
            result = _quiet("prefetch", lambda: self._client.inject(query))
            if result:
                with self._lock:
                    self._prefetch_result = f"[Foresight Memory]\n{result}"

        self._prefetch_thread = threading.Thread(target=_run, name="foresight-prefetch", daemon=True)
        self._prefetch_thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=2.0)
        with self._lock:
            result, self._prefetch_result = self._prefetch_result, ""
        return result

    # -- Capture ---------------------------------------------------------------

    def _post_transcript(self, messages: list[dict], session_id: str) -> None:
        if not self._client or not messages:
            return
        sid = session_id or self._session_id or "hermes"
        _quiet(
            "transcript",
            lambda: self._client.transcript(sid, messages[-40:]),
        )

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict] | None = None,
        **kwargs,
    ) -> None:
        """Distill the completed turn into memory (background, non-blocking)."""
        if not self._client or not user_content:
            return
        turn = [
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ]
        threading.Thread(
            target=self._post_transcript, args=(turn, session_id), name="foresight-turn", daemon=True
        ).start()

    def on_session_end(self, messages: list[dict]) -> None:
        """End-of-session transcript distillation (background)."""
        threading.Thread(
            target=self._post_transcript, args=(messages, self._session_id), name="foresight-session-end", daemon=True
        ).start()

    def on_memory_write(self, action: str, target: str, content: str, **kwargs) -> None:
        """Mirror built-in memory-tool writes into the shared brain."""
        if action != "add" or not content or not self._client:
            return
        category = "preference" if target == "user" else "fact"
        _quiet("memory mirror", lambda: self._client.store(content, category=category))

    # -- Tools -----------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return list(_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if not self._client:
            return tool_error("Foresight provider not initialized")
        try:
            return json.dumps(self._dispatch(tool_name, args))
        except Exception as exc:
            return tool_error(str(exc))

    def _dispatch(self, tool_name: str, args: dict) -> Any:
        if tool_name == "foresight_search":
            return self._client.search(args.get("query", ""), limit=min(int(args.get("limit", 8)), 20))
        if tool_name == "foresight_remember":
            importance = args.get("importance")
            return self._client.store(
                args.get("content", ""),
                category=args.get("category") or "fact",
                importance=float(importance) if importance is not None else 0.5,
            )
        if tool_name == "foresight_context":
            return {"context": self._client.inject(args.get("query", ""))}
        if tool_name == "foresight_forget":
            return self._client.forget(args.get("memory_id", ""))
        if tool_name == "foresight_list":
            return self._client.list_memories(limit=min(int(args.get("limit", 20)), 50))
        return {"error": f"Unknown tool: {tool_name}"}

    def shutdown(self) -> None:
        t = self._prefetch_thread
        if t:
            t.join(timeout=3.0)
        self._client = None


def register(ctx) -> None:
    """Register Foresight as a memory provider plugin."""
    ctx.register_memory_provider(ForesightMemoryProvider())
