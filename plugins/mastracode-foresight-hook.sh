#!/usr/bin/env bash
# MastraCode hook for automatic Foresight context injection and memory capture
set -euo pipefail

FORESIGHT_URL="${FORESIGHT_MCP_URL:-http://127.0.0.1:8764}"

action="${1:-prompt}"
hook_input_file="$(mktemp "${TMPDIR:-/tmp}/foresight-mastra-hook.XXXXXX")" || exit 0
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
    prompt_text = str(hook_input.get("prompt") or hook_input.get("text") or hook_input.get("message") or "")
    try:
        req = urllib.request.Request(
            f"{foresight_url}/ui/api/inject",
            data=json.dumps({"text": prompt_text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
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
- At start of tasks or topic shifts, call `inject_context`.
- When user states preferences or makes architecture decisions, call `manage_context_blocks` or `manage_memories` to persist them.
[/FORESIGHT CONTINUITY CONTEXT]
"""
                    print(output_block.strip())
    except Exception:
        pass

elif "stop" in event_name.lower() or "end" in event_name.lower() or action in ("stop", "idle"):
    session_id = str(hook_input.get("session_id") or "mastracode-session")
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
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
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
                    "Mcp-Name": "process_session_transcript",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=12.0)
        except Exception:
            pass
PY
