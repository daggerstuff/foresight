#!/usr/bin/env python3
"""Standalone Foresight hook: context injection on prompt, transcript capture on stop."""

import http.client
import json
import os
import sys
import urllib.parse
from contextlib import suppress

FORESIGHT_URL = os.environ.get("FORESIGHT_MCP_URL") or "http://127.0.0.1:8764"
DEFAULT_SESSION_ID = "mastracode-session"

PROMPT_DIRECTIVES = (
    "## Standing Memory Directives",
    "You have access to the Foresight persistent memory MCP server.",
    "- Apply the memories and context blocks above naturally.",
    "- At start of tasks or topic shifts, call `inject_context`.",
    "- When user states preferences or makes architecture decisions, call `manage_context_blocks` or "
    "`manage_memories` to persist them.",
)


def _post(url, payload, headers, timeout):
    parts = urllib.parse.urlsplit(url)
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    connection_type = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(parts.hostname or "", parts.port, timeout=timeout)
    try:
        body = json.dumps(payload).encode("utf-8")
        connection.request("POST", target, body=body, headers=headers)
        response = connection.getresponse()
        status, reason = response.status, response.reason
        raw = response.read()
        if status >= http.HTTPStatus.BAD_REQUEST:
            raise RuntimeError(f"HTTP {status}: {reason}")
        return status, raw
    finally:
        connection.close()


action = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else "prompt"
stdin_text = ""
with suppress(Exception):
    stdin_text = sys.stdin.read()

hook_input = {}
if stdin_text.strip():
    with suppress(Exception):
        hook_input = json.loads(stdin_text)

event_name = str(hook_input.get("hook_event_name") or action)

if "prompt" in event_name.lower() or action == "prompt":
    prompt_text = str(hook_input.get("prompt") or hook_input.get("text") or hook_input.get("message") or "")
    with suppress(Exception):
        status, body = _post(
            f"{FORESIGHT_URL}/ui/api/inject",
            {"text": prompt_text},
            {"Content-Type": "application/json"},
            timeout=12.0,
        )
        if status == http.HTTPStatus.OK:
            data = json.loads(body.decode("utf-8"))
            formatted = data.get("formatted", "")
            if formatted:
                output_block = "\n".join(
                    (
                        "[FORESIGHT CONTINUITY CONTEXT]",
                        formatted,
                        "",
                        *PROMPT_DIRECTIVES,
                        "[/FORESIGHT CONTINUITY CONTEXT]",
                    )
                )
                sys.stdout.write(output_block.strip() + "\n")

elif "stop" in event_name.lower() or "end" in event_name.lower() or action in ("stop", "idle"):
    session_id = str(hook_input.get("session_id") or DEFAULT_SESSION_ID)
    messages = hook_input.get("messages") or []
    if messages:
        with suppress(Exception):
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
            _post(
                f"{FORESIGHT_URL}/mcp",
                payload,
                {
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "process_session_transcript",
                },
                timeout=12.0,
            )
