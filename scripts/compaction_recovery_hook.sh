#!/usr/bin/env bash
# Foresight Compaction Recovery Hook — SessionStart
#
# Calls Foresight's compaction_lifecycle MCP tool on session start
# and writes the result to /tmp/foresight-recovery-{session_id}.txt
# for the agent to read on startup.
#
# Graceful degradation: fails silently if Foresight is down.
# Idempotent: overwrites previous recovery file for same session.
# Cleanup: removes recovery files older than 24 hours on each run.
#
# Wiring: add to ~/.mastracode/hooks.json under SessionStart:
#   {
#     "command": "bash /data/vivi/pixelated/foresight/scripts/compaction_recovery_hook.sh",
#     "description": "Foresight compaction recovery on session start",
#     "timeout": 10000,
#     "type": "command"
#   }
#
# Environment:
#   FORESIGHT_API_KEY — API key for Foresight MCP server (if auth enabled)

set -eu

FORESIGHT_URL="http://127.0.0.1:8764/mcp"
RECOVERY_DIR="/tmp"

INPUT="$(cat)"

python3 - "$INPUT" "$FORESIGHT_URL" "$RECOVERY_DIR" <<'PYEOF'
import json
import os
import re
import stat
import sys
import time
import urllib.request
import urllib.error

raw_input = sys.argv[1]
foresight_url = sys.argv[2]
recovery_dir = sys.argv[3]

try:
    hook_input = json.loads(raw_input) if raw_input.strip() else {}
except Exception:
    sys.exit(0)

session_id = hook_input.get("session_id")
if not session_id or not isinstance(session_id, str):
    sys.exit(0)

# Sanitize session_id — allow only [A-Za-z0-9_-] to prevent path traversal
if not re.fullmatch(r'[A-Za-z0-9_-]+', session_id):
    sys.stderr.write(f"Invalid session_id format: {session_id!r}\n")
    sys.exit(0)

payload = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "compaction_lifecycle",
        "arguments": {
            "session_id": session_id,
            "messages": [],
        },
    },
}).encode()

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

api_key = os.environ.get("FORESIGHT_API_KEY")
if api_key:
    headers["Authorization"] = f"Bearer {api_key}"

req = urllib.request.Request(foresight_url, data=payload, headers=headers)

try:
    with urllib.request.urlopen(req, timeout=8) as resp:
        body = resp.read().decode()
        content_type = resp.headers.get("Content-Type", "")
except (urllib.error.URLError, TimeoutError, ConnectionError):
    sys.exit(0)

recovery_text = None

# Handle JSON response body (Content-Type: application/json)
if "application/json" in content_type and not body.lstrip().startswith("data:"):
    try:
        rpc = json.loads(body)
        result = rpc.get("result", {})
        for item in result.get("content", []):
            if item.get("type") == "text":
                recovery_text = item.get("text", "")
                break
    except (json.JSONDecodeError, KeyError, TypeError):
        pass

# Handle SSE response (Content-Type: text/event-stream or data: lines)
if recovery_text is None:
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                rpc = json.loads(line[5:].strip())
                result = rpc.get("result", {})
                for item in result.get("content", []):
                    if item.get("type") == "text":
                        recovery_text = item.get("text", "")
                        break
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            if recovery_text:
                break

if recovery_text:
    output_path = os.path.join(recovery_dir, f"foresight-recovery-{session_id}.txt")
    # Write with restrictive permissions (0o600) — readable only by owner
    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, recovery_text.encode())
    finally:
        os.close(fd)

# Cleanup: remove recovery files older than 24 hours
try:
    now = time.time()
    for f in os.listdir(recovery_dir):
        if f.startswith("foresight-recovery-") and f.endswith(".txt"):
            path = os.path.join(recovery_dir, f)
            if os.path.isfile(path) and os.stat(path).st_mtime < now - 86400:
                os.unlink(path)
except OSError:
    pass
PYEOF
