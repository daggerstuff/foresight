# Foresight plugin for Claude

Persistent memory for Claude — via the [Foresight](https://github.com/daggerstuff/foresight)
MCP server. Injects continuity context on every prompt, captures session
transcripts on stop/compact, and teaches Claude the Foresight skill.

## Contents

| Path | Purpose |
| --- | --- |
| `.claude-plugin/plugin.json` | Claude Code plugin manifest |
| `.mcp.json` | Registers the `foresight` MCP server (`http://127.0.0.1:8764/mcp`) |
| `hooks/hooks.json` | UserPromptSubmit / PreCompact / PostCompact / Stop / SessionEnd hooks |
| `hooks/foresight-hook.sh` | Hook implementation (context injection + transcript capture) |
| `skills/foresight/SKILL.md` | Teaches Claude every Foresight MCP tool and best practices |
| `install.sh` | One-shot installer for Claude Code **and** Claude Desktop |

## Quick install

Prerequisite: the Foresight server must be running
(`bash install.sh` at the repo root, or `foresight-server --port 8764`).

```bash
bash plugins/claude/install.sh
```

That single command:

- **Claude Code** — copies the hook + skill into `~/.claude/`, registers the
  `foresight` MCP server in `~/.claude.json`, and merges the hooks into
  `~/.claude/settings.json` (idempotent — safe to re-run).
- **Claude Desktop** — merges the MCP server into
  `claude_desktop_config.json` for your platform (restart the app after).

### Options

```bash
bash plugins/claude/install.sh --claude-code   # Claude Code only
bash plugins/claude/install.sh --desktop       # Claude Desktop only
bash plugins/claude/install.sh --stdio         # Desktop via stdio (foresight-server)
bash plugins/claude/install.sh --url http://host:8764/mcp
bash plugins/claude/install.sh --dry-run       # preview changes
```

Environment overrides: `FORESIGHT_MCP_URL` sets the default endpoint.

## Install as a real Claude Code plugin

This directory is a valid plugin, so you can also use Claude Code's plugin
system instead of the script:

```
/plugin marketplace add /path/to/foresight/plugins/claude
/plugin install foresight@foresight
```

or add the directory to `CLAUDE_CODE_PLUGIN_DIRS` in `~/.claude/settings.json`.

## Notes

- All hook calls fail silently when the server is down — Claude keeps working,
  memory just pauses until Foresight is back.
- `FORESIGHT_HOOK_MODE=cache` (default) keeps hook captures in a local
  ephemeral cache; set `FORESIGHT_HOOK_MODE=direct` to write straight into
  Foresight on every prompt/stop.
- Hook timeouts: 15s inject, 20s compact — they never block the session.
