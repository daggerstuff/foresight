#!/usr/bin/env bash
# Claude Code hook for automatic Foresight context injection and background capture
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
import urllib.request
import urllib.error

action = sys.argv[1]
input_path = sys.argv[2]
foresight_url = sys.argv[3]

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

if "prompt" in event_name.lower() or action == "prompt":
    prompt_text = str(hook_input.get("prompt") or hook_input.get("text") or "")
    if prompt_text.strip():
        try:
            req = urllib.request.Request(
                f"{foresight_url}/ui/api/inject",
                data=json.dumps({"text": prompt_text}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    formatted = data.get("formatted", "")
                    if formatted:
                        # Output for Claude Code system context
                        print(f"\n[FORESIGHT CONTINUITY CONTEXT]\n{formatted}\n[/FORESIGHT CONTINUITY CONTEXT]\n")
        except Exception:
            pass

elif "stop" in event_name.lower() or action == "stop":
    session_id = str(hook_input.get("session_id") or "claude-session")
    messages = hook_input.get("messages") or []
    if messages:
        try:
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "process_session_transcript",
                    "arguments": {
                        "session_id": session_id,
                        "messages": messages[-10:],
                    },
                },
            }
            req = urllib.request.Request(
                f"{foresight_url}/mcp",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/call",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=4.0)
        except Exception:
            pass
PY
