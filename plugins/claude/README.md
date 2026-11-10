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

Environment overrides: `FORESIGHT_MCP_URL`, `FORESIGHT_API_KEY`,
`FORESIGHT_IDENTITY`.

## Multi-user / remote server setup

Point any number of users at a shared Foresight deployment (e.g.
`https://foresight.example.com`) — each gets their own memory lane while
sharing context blocks:

**On the server** (once per user):

```bash
foresight user create --username alice --email alice@example.com
# prints a one-time API key
foresight user list            # audit view, never prints keys
foresight user rotate-key --username alice
```

**On each user's machine:**

```bash
bash plugins/claude/install.sh \
  --url https://foresight.example.com/mcp \
  --api-key <their-key> \
  --identity alice            # or alice@acme for account scoping
```

The installer writes:

- `~/.config/foresight/credentials.env` (chmod 600) — read by the hooks for
  `Authorization: Bearer` + `X-User-Id` on every call.
- `~/.claude/settings.json` `env` — feeds `${FORESIGHT_API_KEY}` expansion in
  the MCP `headers` block, so the key never sits in `~/.claude.json`.
- `claude_desktop_config.json` gets a literal `Authorization` header (that
  file doesn't support env expansion — it's user-local, mode 600 recommended).

**How identity works:** `X-User-Id` / `user_id` tool args select the memory
lane; `FORESIGHT_IDENTITY=user@account` splits user vs. account/tenant.
Context blocks remain shared across users by default.

**Auth coverage:** the same API key works on `/mcp` (the OAuth provider
accepts API keys as bearer tokens), the REST API (`/memories`, `/inject`,
`/session/transcript`, …), and the `/ui/api/*` endpoints the hooks use —
which are now auth-gated when `FORESIGHT_REQUIRE_API_KEY`/`_OAUTH_BASE_URL`
is set (previously open). claude.ai web/mobile users can instead add a custom
connector and complete OAuth — see `CONNECTOR.md`.

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
