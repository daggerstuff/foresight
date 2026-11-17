#!/usr/bin/env python3
"""Claude Code hook for automatic Foresight context injection and continuous in-flight capture."""

import http.client
import json
import os
import sys
import time
import urllib.parse
from contextlib import suppress

FORESIGHT_URL = os.environ.get("FORESIGHT_MCP_URL") or "http://127.0.0.1:8764"
DEFAULT_SESSION_ID = "claude-session"
DEFAULT_CACHE_TTL_HOURS = 72
DEFAULT_CACHE_MAX_BYTES = 2 * 1024 * 1024
TRANSCRIPT_TAIL_LINES = 60
CACHE_RECENT_ENTRIES = 8

# Secondary switch: "cache" routes hook captures to a local ephemeral cache
# instead of writing into the main Foresight store. "direct" restores the
# original write-to-Foresight behavior.
MODE = (os.environ.get("FORESIGHT_HOOK_MODE") or "cache").strip().lower()
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "foresight")
CACHE_PATH = os.path.join(CACHE_DIR, "hook-cache.jsonl")
CACHE_TTL_HOURS = float(os.environ.get("FORESIGHT_CACHE_TTL_HOURS") or DEFAULT_CACHE_TTL_HOURS)
CACHE_MAX_BYTES = int(os.environ.get("FORESIGHT_CACHE_MAX_BYTES") or DEFAULT_CACHE_MAX_BYTES)

# -- Credentials -------------------------------------------------------------
# Resolution order: environment -> ~/.config/foresight/credentials.env
# (written by plugins/claude/install.sh; KEY=VALUE, chmod 600).
CREDS_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "foresight",
    "credentials.env",
)
CREDS = {}

PARSE_FAILED = object()

PROMPT_DIRECTIVES = (
    "## Standing Memory Directives",
    "You have access to the Foresight persistent memory MCP server.",
    "- Apply the memories and context blocks above naturally.",
    "- Continuous In-Flight Capture: Store key decisions, bug fixes, operational findings, and user "
    "preferences IMMEDIATELY at any point during tasks using `capture_in_flight_memory` or "
    '`manage_memories(action="store", ...)`. Do NOT wait for session wrapup.',
    "- At start of tasks or topic shifts, call `inject_context`.",
    "- When user states preferences or makes architecture decisions, call `manage_context_blocks` or "
    "`capture_in_flight_memory` to persist them immediately.",
)

POSTCOMPACT_DIRECTIVES = (
    "## Standing Memory Directives",
    "You have access to the Foresight persistent memory MCP server.",
    "- Apply the memories and context blocks above naturally.",
    "- Continuous In-Flight Capture: Store key decisions, bug fixes, operational findings, and user "
    "preferences IMMEDIATELY using `capture_in_flight_memory` or "
    '`manage_memories(action="store", ...)`.',
    "- At start of tasks or topic shifts, call `inject_context`.",
    "- When user states preferences or makes architecture decisions, call `manage_context_blocks` or "
    "`capture_in_flight_memory` to persist them immediately.",
)


def _load_credentials():
    with suppress(Exception), open(CREDS_PATH, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                CREDS[key.strip()] = value.strip().strip('"').strip("'")


def _parse_json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return PARSE_FAILED


def _headers():
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    if USER_LANE:
        headers["X-User-Id"] = USER_LANE
    return headers


def _post(url, payload, timeout):
    parts = urllib.parse.urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    connection_type = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parts.hostname or "", parts.port, timeout=timeout)
    try:
        body = json.dumps(payload).encode("utf-8")
        connection.request("POST", target, body=body, headers=_headers())
        response = connection.getresponse()
        status, reason = response.status, response.reason
        raw = response.read()
        if status >= http.HTTPStatus.BAD_REQUEST:
            raise RuntimeError(f"HTTP {status}: {reason}")
        return status, raw
    finally:
        connection.close()


def _msg_text(message):
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _summary_text(hook_input):
    for key in ("summary", "custom_instructions", "compacted_summary"):
        value = hook_input.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _transcript_tail(hook_input, n_lines=TRANSCRIPT_TAIL_LINES):
    """Read the last few entries of the session transcript JSONL.

    Claude Code hook payloads never include the conversation `messages`,
    but they DO include `transcript_path` pointing at the session JSONL.
    Reading the tail gives precompact/stop handlers real content to capture
    (what the session was doing right before the event fired).
    """
    path = hook_input.get("transcript_path")
    if not isinstance(path, str) or not path or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return []
    out = []
    for line in lines[-n_lines:]:
        obj = _parse_json(line)
        if obj is PARSE_FAILED:
            continue
        if obj.get("type") not in ("user", "assistant"):
            continue
        message = obj.get("message")
        text = _msg_text(message) if isinstance(message, dict) else ""
        text = text.strip()
        if text and "FORESIGHT CONTINUITY" not in text:
            out.append(text)
    return out


def _cache_append(entry):
    """Append an entry to the local ephemeral cache (JSONL) and prune it.

    Cache is dumped semi-often: entries older than cache_ttl_hours are
    dropped, and the file is trimmed back to cache_max_bytes from the tail.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    now = time.time()
    entries = []
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    cached = _parse_json(line)
                    if cached is PARSE_FAILED:
                        continue
                    if now - float(cached.get("ts") or 0) < CACHE_TTL_HOURS * 3600:
                        entries.append(cached)
        except Exception:
            entries = []
    entries.append(entry)
    with open(CACHE_PATH, "w", encoding="utf-8") as handle:
        for cached in entries:
            handle.write(json.dumps(cached, ensure_ascii=False) + "\n")
    if os.path.getsize(CACHE_PATH) > CACHE_MAX_BYTES:
        with open(CACHE_PATH, encoding="utf-8") as handle:
            lines = handle.readlines()
        keep_from = len(lines)
        size = 0
        for line in reversed(lines):
            size += len(line)
            if size > CACHE_MAX_BYTES:
                break
            keep_from -= 1
        with open(CACHE_PATH, "w", encoding="utf-8") as handle:
            handle.writelines(lines[keep_from:])


def _cache_recent(n=CACHE_RECENT_ENTRIES):
    """Return the last n cache entries (for postcompact reinjection)."""
    if not os.path.exists(CACHE_PATH):
        return []
    try:
        with open(CACHE_PATH, encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return []
    out = []
    for line in lines[-n:]:
        cached = _parse_json(line)
        if cached is PARSE_FAILED:
            continue
        out.append(cached)
    return out


def _prompt_block(formatted):
    return "\n".join(
        (
            "[FORESIGHT CONTINUITY CONTEXT]",
            formatted,
            "",
            *PROMPT_DIRECTIVES,
            "[/FORESIGHT CONTINUITY CONTEXT]",
        )
    )


_load_credentials()

API_KEY = os.environ.get("FORESIGHT_API_KEY") or CREDS.get("FORESIGHT_API_KEY", "")
_IDENTITY = (
    os.environ.get("FORESIGHT_IDENTITY")
    or CREDS.get("FORESIGHT_IDENTITY", "")
    or os.environ.get("FORESIGHT_USER_ID")
    or CREDS.get("FORESIGHT_USER_ID", "")
)
USER_LANE = _IDENTITY.split("@", 1)[0] if _IDENTITY else ""

action = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "prompt"
stdin_text = ""
with suppress(Exception):
    stdin_text = sys.stdin.read()

hook_input = {}
if stdin_text.strip():
    parsed_input = _parse_json(stdin_text)
    if parsed_input is not PARSE_FAILED:
        hook_input = parsed_input

event_name = str(hook_input.get("hook_event_name") or action)

if action == "capture":
    # Explicit milestone snapshot: reads the transcript tail and stores it.
    # Usage: foresight_hook.py capture  (with hook JSON on stdin, or bare)
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    tail = _transcript_tail(hook_input, n_lines=40)
    text = "\n".join(tail)
    if text:
        if MODE == "direct":
            with suppress(Exception):
                _post(
                    f"{FORESIGHT_URL}/ui/api/capture",
                    {"text": text, "session_id": session_id},
                    timeout=5.0,
                )
        else:
            with suppress(Exception):
                _cache_append(
                    {
                        "ts": time.time(),
                        "kind": "milestone",
                        "session_id": session_id,
                        "text": text[:12000],
                    }
                )

elif "prompt" in event_name.lower() or action == "prompt":
    prompt_text = str(hook_input.get("prompt") or hook_input.get("text") or hook_input.get("message") or "")
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    messages = hook_input.get("messages") or []

    # In-flight capture of prompt/messages continuously (direct mode only;
    # cache mode keeps this in the local ephemeral cache instead)
    if MODE == "direct":
        with suppress(Exception):
            _post(
                f"{FORESIGHT_URL}/ui/api/capture",
                {"text": prompt_text, "messages": messages, "session_id": session_id},
                timeout=3.0,
            )
    else:
        with suppress(Exception):
            if prompt_text:
                _cache_append(
                    {
                        "ts": time.time(),
                        "kind": "prompt",
                        "session_id": session_id,
                        "text": prompt_text[:4000],
                    }
                )

    with suppress(Exception):
        status, body = _post(f"{FORESIGHT_URL}/ui/api/inject", {"text": prompt_text}, timeout=12.0)
        if status == http.HTTPStatus.OK:
            data = json.loads(body.decode("utf-8"))
            formatted = data.get("formatted", "")
            if formatted:
                sys.stdout.write(_prompt_block(formatted) + "\n")

elif "precompact" in event_name.lower() or action == "precompact":
    # Capture the pre-compaction state so it survives the summary.
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    messages = hook_input.get("messages") or []
    tail = _transcript_tail(hook_input)
    capture_text = "\n".join(_msg_text(message) for message in messages[-40:] if _msg_text(message))
    if tail:
        capture_text = (capture_text + "\n" + "\n".join(tail)).strip()
    if MODE == "direct" and capture_text:
        with suppress(Exception):
            _post(
                f"{FORESIGHT_URL}/ui/api/capture",
                {"text": capture_text, "messages": messages[-40:], "session_id": session_id},
                timeout=5.0,
            )
        with suppress(Exception):
            # REST transcript route — works with API-key auth on local and
            # remote (OAuth-fronted) deployments alike.
            transcript_messages = messages[-20:]
            if not transcript_messages and tail:
                transcript_messages = [{"role": "assistant", "content": text} for text in tail[-20:]]
            _post(
                f"{FORESIGHT_URL}/session/transcript",
                {"session_id": session_id, "messages": transcript_messages},
                timeout=12.0,
            )
    else:
        with suppress(Exception):
            if capture_text:
                _cache_append(
                    {
                        "ts": time.time(),
                        "kind": "precompact",
                        "session_id": session_id,
                        "text": capture_text[:12000],
                    }
                )

elif "postcompact" in event_name.lower() or action == "postcompact":
    # Re-inject continuity context right after compaction, keyed on the
    # compaction summary so retrieval is relevant to the surviving context.
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    summary = str(_summary_text(hook_input))
    query = summary[:2000] if summary else "session continuity after compaction"
    with suppress(Exception):
        if summary:
            _cache_append(
                {
                    "ts": time.time(),
                    "kind": "compaction-summary",
                    "session_id": session_id,
                    "text": summary[:6000],
                }
            )
    recent = []
    with suppress(Exception):
        recent = _cache_recent()
    recent_lines = []
    for entry in recent:
        entry_text = str(entry.get("text") or "").strip()
        if entry_text:
            recent_lines.append(f"- [{entry.get('kind', 'note')}] {entry_text[:400]}")
    try:
        status, body = _post(f"{FORESIGHT_URL}/ui/api/inject", {"text": query}, timeout=12.0)
        if status == http.HTTPStatus.OK:
            data = json.loads(body.decode("utf-8"))
            formatted = data.get("formatted", "")
    except Exception:
        formatted = ""
    parts = []
    if formatted:
        parts.append(formatted)
    if recent_lines:
        parts.append("## Recent Local Session Cache (ephemeral, auto-dumped)\n" + "\n".join(recent_lines))
    if parts:
        output_block = "\n\n".join(parts)
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostCompact",
                        "additionalContext": (output_block + "\n" + "\n".join(POSTCOMPACT_DIRECTIVES) + "\n").strip(),
                    }
                }
            )
            + "\n"
        )

elif "stop" in event_name.lower() or "end" in event_name.lower() or action in ("stop", "idle"):
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    messages = hook_input.get("messages") or []
    tail = _transcript_tail(hook_input)
    if MODE == "direct" and (messages or tail):
        with suppress(Exception):
            transcript_messages = (messages or tail)[-10:]
            if transcript_messages and isinstance(transcript_messages[0], str):
                transcript_messages = [{"role": "assistant", "content": text} for text in transcript_messages]
            _post(
                f"{FORESIGHT_URL}/session/transcript",
                {"session_id": session_id, "messages": transcript_messages},
                timeout=12.0,
            )
    elif MODE != "direct":
        with suppress(Exception):
            tail = messages or tail
            if tail:
                _cache_append(
                    {
                        "ts": time.time(),
                        "kind": "stop",
                        "session_id": session_id,
                        "text": "\n".join(tail)[-12000:],
                    }
                )
