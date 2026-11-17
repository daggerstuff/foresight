#!/usr/bin/env python3
"""Config-writing helpers for plugins/claude/install.sh.

Each subcommand reads its inputs from the environment the installer exports and
mutates exactly one file: the hook credentials, ~/.claude.json, the Claude Code
settings, or the Claude Desktop config.
"""

import json
import os
import pathlib
import re
import sys

LEGACY_HOOK_NAME = "foresight-hook.sh"
HOOK_NAME = "foresight_hook.py"


def _load(path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as exc:
        sys.exit(f"cannot parse {path}: {exc}")


def write_credentials():
    p = pathlib.Path(os.environ["CREDS_FILE"])
    updates = {}
    if os.environ.get("API_KEY"):
        updates["FORESIGHT_API_KEY"] = os.environ["API_KEY"]
    if os.environ.get("IDENTITY"):
        updates["FORESIGHT_IDENTITY"] = os.environ["IDENTITY"]

    lines = []
    seen = set()
    if p.exists():
        for line in p.read_text().splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
            if m and m.group(1) in updates:
                lines.append(f"{m.group(1)}={updates[m.group(1)]}")
                seen.add(m.group(1))
            else:
                lines.append(line)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    p.write_text("\n".join(lines).rstrip() + "\n")
    os.chmod(p, 0o600)


def merge_claude_json():
    p = pathlib.Path(os.environ["CLAUDE_JSON"])
    data = _load(p)

    mcp = data.setdefault("mcpServers", {})
    entry = {"type": "http", "url": os.environ["MCP_URL"]}
    # ${FORESIGHT_API_KEY} expands from the Claude Code process env, which we
    # populate via ~/.claude/settings.json "env" below (or the user's shell).
    entry["headers"] = {"Authorization": "Bearer ${FORESIGHT_API_KEY}"}
    mcp["foresight"] = entry
    p.write_text(json.dumps(data, indent=2) + "\n")


def merge_settings():
    p = pathlib.Path(os.environ["SETTINGS"])
    data = _load(p)

    # Feed ${FORESIGHT_API_KEY} expansion in ~/.claude.json mcp headers.
    env = data.setdefault("env", {})
    if os.environ.get("API_KEY"):
        env["FORESIGHT_API_KEY"] = os.environ["API_KEY"]
    if os.environ.get("IDENTITY"):
        env["FORESIGHT_IDENTITY"] = os.environ["IDENTITY"]

    cmd_prefix = os.environ["HOOK_CMD"]
    hooks = data.setdefault("hooks", {})

    def add(event, action, timeout):
        command = f"{cmd_prefix} {action}"
        entries = hooks.setdefault(event, [])
        for entry in entries:
            for hook in entry.get("hooks", []):
                existing = str(hook.get("command", ""))
                if action in existing and (HOOK_NAME in existing or LEGACY_HOOK_NAME in existing):
                    hook["command"] = command
                    return
        matcher = "*" if event == "UserPromptSubmit" else None
        entry = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
        if matcher:
            entry = {"matcher": matcher, **entry}
        entries.append(entry)

    add("UserPromptSubmit", "prompt", 15)
    add("PreCompact", "precompact", 20)
    add("PostCompact", "postcompact", 20)
    add("Stop", "stop", 15)
    add("SessionEnd", "stop", 15)

    p.write_text(json.dumps(data, indent=2) + "\n")


def merge_desktop():
    p = pathlib.Path(os.environ["DESKTOP_CONFIG"])
    data = _load(p)

    mcp = data.setdefault("mcpServers", {})
    if os.environ.get("DESKTOP_STDIO") == "1":
        entry = {"command": "foresight-server"}
        env = {k: v for k, v in os.environ.items() if k.startswith("FORESIGHT_") and v}
        if env:
            entry["env"] = env
        mcp["foresight"] = entry
    else:
        entry = {"type": "http", "url": os.environ["MCP_URL"]}
        api_key = os.environ.get("API_KEY", "")
        if api_key:
            entry["headers"] = {"Authorization": f"Bearer {api_key}"}
        mcp["foresight"] = entry

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n")


COMMANDS = {
    "credentials": write_credentials,
    "claude-json": merge_claude_json,
    "settings": merge_settings,
    "desktop": merge_desktop,
}


if __name__ == "__main__":
    COMMANDS[sys.argv[1]]()
