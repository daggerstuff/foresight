#!/usr/bin/env python3
"""Claude Code hook for automatic Foresight context injection and continuous in-flight capture."""

import hashlib
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.parse
from contextlib import suppress
from typing import Any

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

# Gated recall settings (FOR-1). Opt-in until a before/after A/B against the
# retrieval baseline shows the gate does not suppress relevant recall; the
# /ui/api/telemetry/gating counters exist to collect that evidence.
GATED_RECALL_ENABLED = os.environ.get("FORESIGHT_GATED_RECALL", "false").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
# Threshold source order: explicit env override, then the persisted RRF config
# (foresight rrf config set --gating-threshold ...), then the default.
# Starting threshold: rank-1 fused contributions are ~0.0147 (vector), ~0.0131
# (graph/temporal), ~0.0115 (semantic), ~0.0164 (keyword), so a single-signal
# paraphrase match sits just below this value. Recalibrate per deployment with
# foresight.rrf_tuning.tune_gating_threshold before turning the gate on.
RRF_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".foresight", "rrf_config.json")


def _saved_gating_threshold():
    """Read ``gating_threshold`` from the persisted RRF config, if usable."""
    try:
        with open(RRF_CONFIG_PATH, encoding="utf-8") as handle:
            value = float(json.load(handle).get("gating_threshold"))
    except Exception:
        return None
    return value if math.isfinite(value) and value > 0 else None


_env_gating_threshold = os.environ.get("FORESIGHT_GATED_RECALL_THRESHOLD") or os.environ.get(
    "FORESIGHT_RECALL_THRESHOLD"
)
GATED_RECALL_THRESHOLD = float(_env_gating_threshold) if _env_gating_threshold else (_saved_gating_threshold() or 0.015)
REMINDER_LINE = "[FORESIGHT] Persistent memory active. Call inject_context or search_memories to recall stored context."
WRITE_BACK_NUDGE = (
    "[FORESIGHT WRITE-BACK] Session produced storable decisions or findings. "
    "Consider persisting them with capture_in_flight_memory or manage_memories before ending."
)

_STORABLE_FACT_PATTERNS = [
    re.compile(r"(?i)\b(?:decided|decision:?|agreed to|we decided|architecture decision)\b"),
    re.compile(r"(?i)\b(?:fixed|solution was|root cause|the fix was|bug fix:?|resolved by)\b"),
    re.compile(r"(?i)\b(?:prefer|preference:?|always use|never use|remember to|remember that)\b"),
    re.compile(r"(?i)\b(?:learned that|key finding|takeaway:?)\b"),
]


def _has_storable_facts(texts):
    """Check if transcript texts contain storable facts, decisions, or fixes."""
    for text in texts:
        for p in _STORABLE_FACT_PATTERNS:
            if p.search(text):
                return True
    return False


def _nudge_marker_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]{1,64}", "_", session_id)[:64] or DEFAULT_SESSION_ID
    return os.path.join(CACHE_DIR, f"nudge-{safe}.txt")


def _storable_fact_lines(texts: list[str]) -> list[str]:
    """The lines that matched a storable-fact pattern, plus the line after.

    A decision is often a heading plus its body; fingerprinting only the
    heading would hide a new decision under a repeated heading.
    """
    matches: list[str] = []
    all_lines: list[str] = []
    for text in texts:
        all_lines.extend(text.splitlines())
    for i, line in enumerate(all_lines):
        if any(p.search(line) for p in _STORABLE_FACT_PATTERNS):
            matches.append(line.strip())
            if i + 1 < len(all_lines):
                matches.append(all_lines[i + 1].strip())
    return matches


def _nudge_already_emitted(session_id: str, texts: list[str]) -> bool:
    """True when this exact matching content already produced a nudge.

    The transcript tail persists across Stop events, so the same finding would
    re-emit the nudge on every following turn until it scrolled out of the
    window. The digest covers only the matching lines, so an unrelated
    follow-up does not re-open the nudge.
    """
    digest = hashlib.sha256("\n".join(texts).encode("utf-8", "replace")).hexdigest()
    try:
        with open(_nudge_marker_path(session_id), encoding="utf-8") as handle:
            return handle.read().strip() == digest
    except OSError:
        return False


def _mark_nudge_emitted(session_id: str, texts: list[str]) -> None:
    digest = hashlib.sha256("\n".join(texts).encode("utf-8", "replace")).hexdigest()
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_nudge_marker_path(session_id), "w", encoding="utf-8") as handle:
            handle.write(digest)
    except OSError:
        pass


# -- Credentials -------------------------------------------------------------
# Resolution order: environment -> ~/.config/foresight/credentials.env
# (written by plugins/claude/install.sh; KEY=VALUE, chmod 600).
CREDS_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "foresight",
    "credentials.env",
)
CREDS = {}

PARSE_FAILED: object = object()

PROMPT_DIRECTIVES = (
    "## Standing Memory Directives",
    "You have access to the Foresight persistent memory MCP server.",
    "- Apply the memories and context blocks above naturally.",
    "- Continuous In-Flight Capture: Store key decisions, bug fixes, operational findings, and user "
    "preferences IMMEDIATELY at any point during tasks using `capture_in_flight_memory` or "
    '`manage_memories(params={"action": "store", ...})`. Do NOT wait for session wrapup.',
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
    '`manage_memories(params={"action": "store", ...})`.',
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


def _parse_json(raw: str) -> Any:
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


_SECRET_PATTERNS = [
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\baws_secret_access_key\s*=\s*[A-Za-z0-9/+=]{40}\b"),
    re.compile(r"\bxox[baprs]-[0-9a-zA-Z-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9-_=]+\.eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_.+/=]+\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{20,}\b"),
    re.compile(r"\b(?:postgres|postgresql|mysql|mongodb|redis|amqp|mssql):\/\/[^\s:@]+:[^\s:@]+@[^\s\/]+"),
    re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:api_key|apikey|client_secret|auth_token)[\"']?\s*[:=]\s*"
        r"(?:\"(?:\\[\s\S]|[^\"\\]){8,}\"|'(?:\\[\s\S]|[^'\\]){8,}'|[A-Za-z0-9_\-./+=]{16,})"
    ),
    re.compile(
        r"(?i)(?<![A-Za-z0-9])(?:password|passwd|pwd)[\"']?\s*[:=]\s*"
        r"(?:\"(?:\\[\s\S]|[^\"\\]){8,}\"|'(?:\\[\s\S]|[^'\\]){8,}'|[^\s\"']{8,})"
    ),
]


_PEM_BEGIN_RE = re.compile(r"-----BEGIN ((?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----")
_PEM_END_RE = re.compile(r"-----END ((?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----")
# One line of base64 key material: a header quoted in prose is not followed by
# any, so only the marker itself is replaced there. The final body line can be
# shorter (e.g. 4+ base64 chars).
_PEM_BODY_LINE_RE = re.compile(r"^[A-Za-z0-9+/=]{16,}$")
_PEM_FINAL_BODY_LINE_RE = re.compile(r"^[A-Za-z0-9+/=]{4,}$")


def _matching_pem_end(text: str, start: int, pem_type: str):
    """The first END marker of the *same* key type after *start*."""
    for end in _PEM_END_RE.finditer(text, start):
        if end.group(1) == pem_type:
            return end
    return None


def _pem_body_end(text: str, start: int) -> int:
    """End offset of the base64 body that follows an unterminated header."""
    pos = start
    if text.startswith("\r\n", pos):
        pos += 2
    elif text.startswith("\n", pos):
        pos += 1
    end = pos
    seen_body = False
    while pos < len(text):
        newline = text.find("\n", pos)
        line = text[pos:] if newline == -1 else text[pos:newline]
        stripped = line.strip()
        if _PEM_BODY_LINE_RE.match(stripped):
            seen_body = True
        elif seen_body and _PEM_FINAL_BODY_LINE_RE.match(stripped):
            return len(text) if newline == -1 else newline + 1
        else:
            return end
        pos = len(text) if newline == -1 else newline + 1
        end = pos
    return end


def _scrub_pem_blocks(text: str) -> str:
    """Redact every PEM private-key block with a linear scan.

    A bounded regex body let a key larger than the bound reach the transcript
    untouched. Scanning with ``search`` from a monotonically advancing offset
    recognises the whole block whatever its length. A header with no END is
    redacted only as far as its base64 body, so a quoted marker in prose loses
    nothing else.
    """
    if "PRIVATE KEY" not in text:
        return text
    pieces: list[str] = []
    pos = 0
    while True:
        begin = _PEM_BEGIN_RE.search(text, pos)
        if begin is None:
            pieces.append(text[pos:])
            return "".join(pieces)
        end = _matching_pem_end(text, begin.end(), begin.group(1))
        pieces.append(text[pos : begin.start()])
        pieces.append("[REDACTED-SECRET]")
        if end is not None:
            pos = end.end()
            continue
        body_start = begin.end()
        body_end = _pem_body_end(text, body_start)
        if body_end <= body_start + 2:
            pos = body_start
            continue
        pos = body_end


def _scrub_secrets(text: str) -> str:
    """Scrub sensitive credentials from captured hook text (FOR-46)."""
    out = _scrub_pem_blocks(text)
    for p in _SECRET_PATTERNS:
        out = p.sub("[REDACTED-SECRET]", out)
    return out


def _scrub_messages(messages):
    """Return message copies with every text part scrubbed before transport."""
    if not isinstance(messages, list):
        return []
    cleaned = []
    for message in messages:
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        content = message.get("content")
        if isinstance(content, str):
            cleaned.append({**message, "content": _scrub_secrets(content)})
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append({**part, "text": _scrub_secrets(part["text"])})
                else:
                    parts.append(part)
            cleaned.append({**message, "content": parts})
        else:
            cleaned.append(message)
    return cleaned


def _recall_scores(data):
    """Return (formatted, top_score) from an inject response."""
    formatted = data.get("formatted", "")
    top_score = float(data.get("top_score") or 0.0)
    if not top_score and data.get("memories"):
        top_score = float(data["memories"][0].get("combined_score") or 0.0)
    return formatted, top_score


def _should_gate(top_score: float) -> bool:
    return GATED_RECALL_ENABLED and top_score < GATED_RECALL_THRESHOLD


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
            out.append(_scrub_secrets(text))
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


def _handle_capture(hook_input: dict[str, Any]) -> None:
    """Explicit milestone snapshot: reads the transcript tail and stores it."""
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    tail = _transcript_tail(hook_input, n_lines=40)
    text = "\n".join(tail)
    if not text:
        return
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


def _handle_prompt(hook_input: dict[str, Any]) -> None:
    """Process user prompt, capture in-flight, and inject continuity context."""
    raw_prompt = str(hook_input.get("prompt") or hook_input.get("text") or hook_input.get("message") or "")
    prompt_text = _scrub_secrets(raw_prompt)
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    messages = _scrub_messages(hook_input.get("messages") or [])

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
            formatted, top_score = _recall_scores(data)
            static_and_safety = str(data.get("static_and_safety") or "").strip()
            decision = "gated" if _should_gate(top_score) else "injected"

            with suppress(Exception):
                _post(
                    f"{FORESIGHT_URL}/ui/api/telemetry/gating",
                    {
                        "surface": "claude_hook",
                        "decision": decision,
                        "top_score": top_score,
                        "threshold": GATED_RECALL_THRESHOLD,
                    },
                    timeout=2.0,
                )

            if decision == "gated":
                parts = []
                if static_and_safety:
                    parts.append(static_and_safety)
                parts.append(REMINDER_LINE)
                gated_block = "\n\n".join(parts)
                sys.stdout.write(
                    "\n".join(
                        (
                            "[FORESIGHT CONTINUITY CONTEXT]",
                            gated_block,
                            "[/FORESIGHT CONTINUITY CONTEXT]",
                        )
                    )
                    + "\n"
                )
            elif formatted:
                sys.stdout.write(_prompt_block(formatted) + "\n")


def _handle_precompact(hook_input: dict[str, Any]) -> None:
    """Capture pre-compaction state so it survives summarization."""
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    messages = _scrub_messages(hook_input.get("messages") or [])
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


def _handle_postcompact(hook_input: dict[str, Any]) -> None:
    """Re-inject continuity context right after compaction."""
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    summary = _scrub_secrets(str(_summary_text(hook_input)))
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
    formatted = ""
    static_and_safety = ""
    gated = False
    try:
        status, body = _post(f"{FORESIGHT_URL}/ui/api/inject", {"text": query}, timeout=12.0)
        if status == http.HTTPStatus.OK:
            data = json.loads(body.decode("utf-8"))
            formatted, top_score = _recall_scores(data)
            static_and_safety = str(data.get("static_and_safety") or "").strip()
            gated = _should_gate(top_score)
    except Exception:
        formatted = ""
    parts = []
    if gated:
        if static_and_safety:
            parts.append(static_and_safety)
        parts.append(REMINDER_LINE)
    elif formatted:
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


def _handle_stop(hook_input: dict[str, Any]) -> None:
    """Process stop/idle event and emit write-back nudge if findings were generated."""
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    messages = _scrub_messages(hook_input.get("messages") or [])
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
            stop_text = "\n".join(t for t in tail if isinstance(t, str))
            if stop_text:
                _cache_append(
                    {
                        "ts": time.time(),
                        "kind": "stop",
                        "session_id": session_id,
                        "text": stop_text[-12000:],
                    }
                )

    texts_to_check = []
    if isinstance(tail, list):
        texts_to_check.extend(t for t in tail if isinstance(t, str))
    if isinstance(messages, list):
        texts_to_check.extend(_msg_text(m) for m in messages if isinstance(m, dict))
    fact_lines = _storable_fact_lines(texts_to_check)
    if fact_lines and not _nudge_already_emitted(session_id, fact_lines):
        # Claude Code surfaces Stop-hook stdout only as a JSON systemMessage.
        sys.stdout.write(json.dumps({"systemMessage": WRITE_BACK_NUDGE}) + "\n")
        _mark_nudge_emitted(session_id, fact_lines)


def _resolve_action(argv: list[str] | None) -> str:
    """Determine the action string from argv."""
    args = sys.argv if argv is None else argv
    if len(args) > 1 and args[1]:
        return args[1]
    if len(args) == 1 and not args[0].endswith(".py"):
        return args[0]
    return "prompt"


def _read_hook_input(stdin_text: str | None) -> dict[str, Any]:
    """Read and parse json from stdin if available."""
    text_in = stdin_text or ""
    if stdin_text is None:
        with suppress(Exception):
            text_in = sys.stdin.read()
    if not text_in.strip():
        return {}
    parsed = _parse_json(text_in)
    return parsed if parsed is not PARSE_FAILED else {}


def _dispatch_hook(action: str, hook_input: dict[str, Any]) -> None:
    """Route parsed hook payload to its event handler."""
    event_name = str(hook_input.get("hook_event_name") or action).lower()
    if action == "capture":
        _handle_capture(hook_input)
    elif "prompt" in event_name or (action == "prompt" and not hook_input.get("hook_event_name")):
        _handle_prompt(hook_input)
    elif "precompact" in event_name or action == "precompact":
        _handle_precompact(hook_input)
    elif "postcompact" in event_name or action == "postcompact":
        _handle_postcompact(hook_input)
    elif "stop" in event_name or "end" in event_name or action in ("stop", "idle"):
        _handle_stop(hook_input)


def main(argv: list[str] | None = None, stdin_text: str | None = None) -> None:
    """Dispatch one hook event from argv and stdin.

    Importing the module only loads configuration (env, credentials file,
    persisted RRF threshold); the dispatcher and any network call run here.
    """
    action = _resolve_action(argv)
    hook_input = _read_hook_input(stdin_text)
    _dispatch_hook(action, hook_input)


if __name__ == "__main__":
    main()
