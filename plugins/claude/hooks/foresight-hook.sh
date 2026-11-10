#!/usr/bin/env bash
# Claude Code hook for automatic Foresight context injection and continuous in-flight capture
set -euo pipefail

FORESIGHT_URL="${FORESIGHT_MCP_URL:-http://127.0.0.1:8764}"

action="${1:-prompt}"
hook_input_file="$(mktemp "${TMPDIR:-/tmp}/foresight-claude-hook.XXXXXX")" || exit 0
trap 'rm -f "$hook_input_file"' EXIT HUP INT TERM
cat >"$hook_input_file" 2>/dev/null || true

python3 - <<'PY' "$action" "$hook_input_file" "$FORESIGHT_URL"
import json
import os
import sys
import time
import urllib.request
import urllib.error

action = sys.argv[1]
input_path = sys.argv[2]
foresight_url = sys.argv[3]

# Secondary switch: "cache" routes hook captures to a local ephemeral cache
# instead of writing into the main Foresight store. "direct" restores the
# original write-to-Foresight behavior.
mode = (os.environ.get("FORESIGHT_HOOK_MODE") or "cache").strip().lower()
cache_dir = os.path.join(
    os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "foresight"
)
cache_path = os.path.join(cache_dir, "hook-cache.jsonl")
cache_ttl_hours = float(os.environ.get("FORESIGHT_CACHE_TTL_HOURS") or 72)
cache_max_bytes = int(os.environ.get("FORESIGHT_CACHE_MAX_BYTES") or (2 * 1024 * 1024))


# -- Credentials -------------------------------------------------------------
# Resolution order: environment -> ~/.config/foresight/credentials.env
# (written by plugins/claude/install.sh; KEY=VALUE, chmod 600).
_creds = {}
_creds_path = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "foresight", "credentials.env",
)
try:
    with open(_creds_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                _creds[_k.strip()] = _v.strip().strip('"').strip("'")
except Exception:
    pass

api_key = os.environ.get("FORESIGHT_API_KEY") or _creds.get("FORESIGHT_API_KEY", "")
_identity = (
    os.environ.get("FORESIGHT_IDENTITY")
    or _creds.get("FORESIGHT_IDENTITY", "")
    or os.environ.get("FORESIGHT_USER_ID")
    or _creds.get("FORESIGHT_USER_ID", "")
)
user_lane = _identity.split("@", 1)[0] if _identity else ""


def _headers():
    h = {"Content-Type": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    if user_lane:
        h["X-User-Id"] = user_lane
    return h


def _msg_text(m):
    if not isinstance(m, dict):
        return ""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(
            str(x.get("text") or "")
            for x in c
            if isinstance(x, dict) and x.get("type") == "text"
        )
    return ""


def _summary_text(hi):
    for key in ("summary", "custom_instructions", "compacted_summary"):
        val = hi.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _transcript_tail(hi, n_lines=60):
    """Read the last few entries of the session transcript JSONL.

    Claude Code hook payloads never include the conversation `messages`,
    but they DO include `transcript_path` pointing at the session JSONL.
    Reading the tail gives precompact/stop handlers real content to capture
    (what the session was doing right before the event fired).
    """
    path = hi.get("transcript_path")
    if not isinstance(path, str) or not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    for line in lines[-n_lines:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") not in ("user", "assistant"):
            continue
        msg = obj.get("message")
        text = _msg_text(msg) if isinstance(msg, dict) else ""
        text = text.strip()
        if text and "FORESIGHT CONTINUITY" not in text:
            out.append(text)
    return out


def _cache_append(entry):
    """Append an entry to the local ephemeral cache (JSONL) and prune it.

    Cache is dumped semi-often: entries older than cache_ttl_hours are
    dropped, and the file is trimmed back to cache_max_bytes from the tail.
    """
    os.makedirs(cache_dir, exist_ok=True)
    now = time.time()
    entries = []
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if now - float(e.get("ts") or 0) < cache_ttl_hours * 3600:
                        entries.append(e)
        except Exception:
            entries = []
    entries.append(entry)
    with open(cache_path, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    if os.path.getsize(cache_path) > cache_max_bytes:
        with open(cache_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        keep_from = len(lines)
        size = 0
        for e in reversed(lines):
            size += len(e)
            if size > cache_max_bytes:
                break
            keep_from -= 1
        with open(cache_path, "w", encoding="utf-8") as f:
            f.writelines(lines[keep_from:])


def _cache_recent(n=8):
    """Return the last n cache entries (for postcompact reinjection)."""
    if not os.path.exists(cache_path):
        return []
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception:
        return []
    out = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


hook_input = {}
if os.path.exists(input_path):
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            content = f.read()
            if content.strip():
                hook_input = json.loads(content)
    except Exception:
        hook_input = {}

event_name = str(hook_input.get("hook_event_name") or action)

if action == "capture":
    # Explicit milestone snapshot: reads the transcript tail and stores it.
    # Usage: foresight-hook.sh capture  (with hook JSON on stdin, or bare)
    session_id = str(hook_input.get("session_id") or "claude-session")
    tail = _transcript_tail(hook_input, n_lines=40)
    text = "\n".join(tail)
    if text:
        if mode == "direct":
            try:
                req = urllib.request.Request(
                    f"{foresight_url}/ui/api/capture",
                    data=json.dumps({"text": text, "session_id": session_id}).encode("utf-8"),
                    headers=_headers(),
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=5.0)
            except Exception:
                pass
        else:
            try:
                _cache_append({
                    "ts": time.time(),
                    "kind": "milestone",
                    "session_id": session_id,
                    "text": text[:12000],
                })
            except Exception:
                pass

elif "prompt" in event_name.lower() or action == "prompt":
    prompt_text = str(hook_input.get("prompt") or hook_input.get("text") or hook_input.get("message") or "")
    session_id = str(hook_input.get("session_id") or "claude-session")
    messages = hook_input.get("messages") or []

    # In-flight capture of prompt/messages continuously (direct mode only;
    # cache mode keeps this in the local ephemeral cache instead)
    if mode == "direct":
        try:
            req = urllib.request.Request(
                f"{foresight_url}/ui/api/capture",
                data=json.dumps({"text": prompt_text, "messages": messages, "session_id": session_id}).encode("utf-8"),
                headers=_headers(),
                method="POST",
            )
            urllib.request.urlopen(req, timeout=3.0)
        except Exception:
            pass
    else:
        try:
            if prompt_text:
                _cache_append({
                    "ts": time.time(),
                    "kind": "prompt",
                    "session_id": session_id,
                    "text": prompt_text[:4000],
                })
        except Exception:
            pass

    try:
        req = urllib.request.Request(
            f"{foresight_url}/ui/api/inject",
            data=json.dumps({"text": prompt_text}).encode("utf-8"),
            headers=_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12.0) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                formatted = data.get("formatted", "")
                if formatted:
                    output_block = f"""
[FORESIGHT CONTINUITY CONTEXT]
{formatted}

## Standing Memory Directives
You have access to the Foresight persistent memory MCP server.
- Apply the memories and context blocks above naturally.
- Continuous In-Flight Capture: Store key decisions, bug fixes, operational findings, and user preferences IMMEDIATELY at any point during tasks using `capture_in_flight_memory` or `manage_memories(action="store", ...)`. Do NOT wait for session wrapup.
- At start of tasks or topic shifts, call `inject_context`.
- When user states preferences or makes architecture decisions, call `manage_context_blocks` or `capture_in_flight_memory` to persist them immediately.
[/FORESIGHT CONTINUITY CONTEXT]
"""
                    print(output_block.strip())
    except Exception:
        pass

elif "precompact" in event_name.lower() or action == "precompact":
    # Capture the pre-compaction state so it survives the summary.
    session_id = str(hook_input.get("session_id") or "claude-session")
    messages = hook_input.get("messages") or []
    tail = _transcript_tail(hook_input)
    capture_text = "\n".join(
        _msg_text(m) for m in messages[-40:] if _msg_text(m)
    )
    if tail:
        capture_text = (capture_text + "\n" + "\n".join(tail)).strip()
    if mode == "direct" and capture_text:
        try:
            req = urllib.request.Request(
                f"{foresight_url}/ui/api/capture",
                data=json.dumps({"text": capture_text, "messages": messages[-40:], "session_id": session_id}).encode("utf-8"),
                headers=_headers(),
                method="POST",
            )
            urllib.request.urlopen(req, timeout=5.0)
        except Exception:
            pass
        try:
            # REST transcript route — works with API-key auth on local and
            # remote (OAuth-fronted) deployments alike.
            transcript_messages = messages[-20:]
            if not transcript_messages and tail:
                transcript_messages = [{"role": "assistant", "content": t} for t in tail[-20:]]
            req = urllib.request.Request(
                f"{foresight_url}/session/transcript",
                data=json.dumps({
                    "session_id": session_id,
                    "messages": transcript_messages,
                }).encode("utf-8"),
                headers=_headers(),
                method="POST",
            )
            urllib.request.urlopen(req, timeout=12.0)
        except Exception:
            pass
    else:
        try:
            if capture_text:
                _cache_append({
                    "ts": time.time(),
                    "kind": "precompact",
                    "session_id": session_id,
                    "text": capture_text[:12000],
                })
        except Exception:
            pass

elif "postcompact" in event_name.lower() or action == "postcompact":
    # Re-inject continuity context right after compaction, keyed on the
    # compaction summary so retrieval is relevant to the surviving context.
    session_id = str(hook_input.get("session_id") or "claude-session")
    summary = str(_summary_text(hook_input))
    query = summary[:2000] if summary else "session continuity after compaction"
    try:
        if summary:
            _cache_append({
                "ts": time.time(),
                "kind": "compaction-summary",
                "session_id": session_id,
                "text": summary[:6000],
            })
    except Exception:
        pass
    recent = []
    try:
        recent = _cache_recent(8)
    except Exception:
        recent = []
    recent_lines = []
    for e in recent:
        t = str(e.get("text") or "").strip()
        if t:
            recent_lines.append(f"- [{e.get('kind', 'note')}] {t[:400]}")
    try:
        req = urllib.request.Request(
            f"{foresight_url}/ui/api/inject",
            data=json.dumps({"text": query}).encode("utf-8"),
            headers=_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12.0) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
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
        directives = """
## Standing Memory Directives
You have access to the Foresight persistent memory MCP server.
- Apply the memories and context blocks above naturally.
- Continuous In-Flight Capture: Store key decisions, bug fixes, operational findings, and user preferences IMMEDIATELY using `capture_in_flight_memory` or `manage_memories(action="store", ...)`.
- At start of tasks or topic shifts, call `inject_context`.
- When user states preferences or makes architecture decisions, call `manage_context_blocks` or `capture_in_flight_memory` to persist them immediately.
"""
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostCompact",
                "additionalContext": (output_block + directives).strip(),
            }
        }))

elif "stop" in event_name.lower() or "end" in event_name.lower() or action in ("stop", "idle"):
    session_id = str(hook_input.get("session_id") or "claude-session")
    messages = hook_input.get("messages") or []
    tail = _transcript_tail(hook_input)
    if mode == "direct" and (messages or tail):
        try:
            transcript_messages = (messages or tail)[-10:]
            if transcript_messages and isinstance(transcript_messages[0], str):
                transcript_messages = [{"role": "assistant", "content": t} for t in transcript_messages]
            req = urllib.request.Request(
                f"{foresight_url}/session/transcript",
                data=json.dumps({
                    "session_id": session_id,
                    "messages": transcript_messages,
                }).encode("utf-8"),
                headers=_headers(),
                method="POST",
            )
            urllib.request.urlopen(req, timeout=12.0)
        except Exception:
            pass
    elif mode != "direct":
        try:
            tail = messages or tail
            if tail:
                _cache_append({
                    "ts": time.time(),
                    "kind": "stop",
                    "session_id": session_id,
                    "text": "\n".join(tail)[-12000:],
                })
        except Exception:
            pass
PY
