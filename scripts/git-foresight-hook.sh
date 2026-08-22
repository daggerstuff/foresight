#!/usr/bin/env bash
# Git post-commit hook for automatic Foresight memory capture
# Silently captures commit messages and changed files into Foresight in the background.

FORESIGHT_URL="${FORESIGHT_MCP_URL:-http://127.0.0.1:8764}"

# Only run if git repo has commits
COMMIT_HASH="$(git log -1 --format="%h" 2>/dev/null || true)"
COMMIT_MSG="$(git log -1 --format="%s" 2>/dev/null || true)"
COMMIT_AUTHOR="$(git log -1 --format="%an" 2>/dev/null || true)"
REPO_NAME="$(basename "$(git rev-parse --show-toplevel 2>/dev/null || pwd)")"

if [ -z "$COMMIT_HASH" ] || [ -z "$COMMIT_MSG" ]; then
  exit 0
fi

# Do not loop on automatic commits
if [[ "$COMMIT_MSG" == *"[skip-foresight]"* ]]; then
  exit 0
fi

# Asynchronously send memory to Foresight in background
(
  python3 -c '
import sys, json, urllib.request

url = sys.argv[1]
repo = sys.argv[2]
chash = sys.argv[3]
msg = sys.argv[4]
author = sys.argv[5]

content = f"[{repo}] Commit {chash} by {author}: {msg}"
payload = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {
        "name": "manage_memories",
        "arguments": {
            "action": "store",
            "content": content,
            "category": "decision",
            "scope": "arc",
            "retention": "medium_term",
            "importance": 0.6
        }
    }
}

try:
    req = urllib.request.Request(
        f"{url}/mcp",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call"
        },
        method="POST"
    )
    urllib.request.urlopen(req, timeout=3.0)
except Exception:
    pass
' "$FORESIGHT_URL" "$REPO_NAME" "$COMMIT_HASH" "$COMMIT_MSG" "$COMMIT_AUTHOR"
) &>/dev/null &
disown
exit 0
