# Foresight 🧠

**Persistent memory for AI agents** — CLI, TUI, MCP server, and Python SDK.

[![PyPI](https://img.shields.io/pypi/v/foresight?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/foresight/)
[![Python](https://img.shields.io/pypi/pyversions/foresight?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/pypi/l/foresight?color=green)](LICENSE)
[![CI](https://github.com/daggerstuff/foresight/actions/workflows/ci.yml/badge.svg)](https://github.com/daggerstuff/foresight/actions)
[![Downloads](https://img.shields.io/pypi/dm/foresight?color=purple)](https://pypi.org/project/foresight/)

---

### Quick start (one command)

```bash
bash install.sh
```

The installer handles everything: dependencies, database setup, `.env`
generation, schema init, health check, systemd service, OpenCode MCP
auto-config, and your first memory. You just need a Postgres connection
string (or pick a provider from the interactive menu).

**Prefer manual?**

```bash
pip install foresight[all]
export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'
foresight init && foresight doctor && foresight tui
```

---

### What you get

| Surface                 | What                                   | How                                                                     |
| ----------------------- | -------------------------------------- | ----------------------------------------------------------------------- |
| **`foresight`**         | Full CLI with 20+ commands             | `foresight store "hello"`, `foresight list`, `foresight query "search"` |
| **`foresight --agent`** | Machine-parseable output for AI agents | `foresight --agent status → [JSON] {...}`                               |
| **`foresight tui`**     | Interactive Textual TUI                | Browse, search, edit memories — keyboard-first                          |
| **`foresight-server`**  | MCP server for agent tool integration  | Add to Claude Code, Cursor, Goose, any MCP client                       |
| **Python SDK**          | Import directly for custom tooling     | Use Python API helpers for custom scripts                               |

---

### Install walkthrough

#### Step 1 — Install

```bash
bash install.sh
```

Or manually:

```bash
pip install foresight[all]
```

> **Extras breakdown** — install only what you need:
>
> | Extra    | Includes                            |
> | -------- | ----------------------------------- |
> | `(none)` | MCP server only — no CLI, no TUI    |
> | `[cli]`  | CLI (`typer` + `rich`) — no TUI     |
> | `[tui]`  | CLI + TUI (`textual`) — no MCP      |
> | `[all]`  | Everything — CLI + TUI + MCP server |

On macOS/Linux with uv installed, `uv pip install foresight[all]` is ~3x faster.

---

#### Step 2 — Database

Foresight is **Postgres-only**. The installer offers an interactive menu
(Neon, Supabase, Railway, Replit, Local, Other) with signup links.

Or set it manually:

```bash
export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'
```

> **On Replit** — `DATABASE_URL` is injected automatically.

#### Step 3 — Init + Doctor

```bash
foresight init          # Creates config + verifies database
foresight doctor        # Health check — 11 checks including LLM, Redis, MCP
```

Doctor now checks: Python version, config dir/file, DB URL, user/bank ID,
DB responsive, LLM provider, Redis cache, MCP HTTP server, schema version.

#### Step 4 — Store, list, retrieve

```bash
foresight store "First real memory from the CLI walkthrough"
foresight list
foresight query "test"
foresight get <id>
```

#### Step 5 — TUI

```bash
foresight tui
```

Full-screen Textual terminal UI. Three tabs: Dashboard, Memories, Blocks.
Keyboard: `Tab` between tabs, `/` to search, `q` to quit.

#### Step 6 — Agent mode (machine output)

```bash
foresight --agent status    # [JSON] {...}
foresight --json status     # Pure JSON to stdout
```

#### Step 7 — Wire it to your AI agent

Add to any MCP-compatible agent. `FORESIGHT_DB_URL` is required in every
client config. Here's the Claude Code config:

```json
// ~/.claude.json or claude_desktop_config.json
{
  "mcpServers": {
    "foresight": {
      "command": "uvx",
      "args": ["foresight-server"],
      "env": {
        "FORESIGHT_DB_URL": "postgresql://user:pass@host:5432/db?sslmode=require",
        "FORESIGHT_IDENTITY": "your-username"
      }
    }
  }
}
```

**Cursor** → Settings → MCP Servers → Add new:

```bash
Command: uvx
Arguments: foresight-server
Env: FORESIGHT_DB_URL=postgresql://user:pass@host:5432/db?sslmode=require
```

**Goose** — same pattern, same `env` block. Any stdio MCP client works.

Once connected, your agent gets Foresight as a built-in tool. It can store
memories from conversations, search across everything you've told it, and pull
context from three sessions ago — automatically, without you asking.

---

### Quick reference

```bash
# Store & retrieve
foresight store "text"                    # Store a memory
foresight get <id>                         # Get memory by ID
foresight list                             # List all memories (newest first)
foresight query "search term"              # Keyword + hybrid search
foresight search "term"                    # Advanced search with signals/scoring

# Analysis
foresight synthesize                      # Find patterns & contradictions
foresight reflect --period weekly          # Time-windowed reflection
foresight profile                          # Build user profile (static + dynamic)

# Data portability
foresight export memories.json             # Export to JSON file
foresight import memories.json             # Import from JSON file

# System
foresight doctor                           # 7-point diagnostics
foresight stats                            # Memory count, scope breakdown
foresight config                           # View/set config values
foresight init --force                     # Reinitialize (wipes data)

# Output modes
foresight --agent status                  # Machine-parseable: [JSON] {...}
foresight --json status                   # Pure JSON to stdout
foresight -o json status                  # Same as --json (short form)

# TUI
foresight tui                             # Full-screen Textual terminal UI
```

---

### Extras

- **Shell completion**: `foresight --install-completion`
  - **Database URL**: `export FORESIGHT_DB_URL=postgresql://user:pass@host:5432/foresight`
- **Config file**: `~/.foresight/config.json`
- **Docker databases**: See [Installation Guide](https://foresight.vectorize.io/installation)

---

## Architecture

Foresight is **Postgres-only** — there is no local SQLite store. `FORESIGHT_DB_URL`
must point at the shared Ghost Postgres instance; the daemon fails fast if it is
unset.

Foresight combines three layers:

### Core memory system

- **Structured memory storage** with scope, retention, tags, and emotional
  metadata
- **Safety-aware ingestion** with crisis detection and gate decisions
- **Synthesis and reflection** pipelines for trends, contradictions, and stance
  shifts
- **Versioning and archival** for long-lived memory maintenance

### Context blocks

Context blocks are the Foresight-native continuity surface for active guidance
and project state. They are persisted in PostgreSQL (shared Ghost Postgres) and isolated by
`(user_id, tenant_id)`, so the same user can carry different continuity state
across tenants without leakage.

Default blocks:

- `core_directives`
- `guidance`
- `pending_items`
- `project_context`
- `session_patterns`
- `user_preferences`
- `self_improvement`
- `tool_guidelines`

### Curation runs

Curation runs are asynchronous jobs that reorganize an existing memory bank into
either a separate reviewable output bank or, when explicitly allowed, back into
the source bank through a staging-and-promotion flow.

- **Source bank preserved** by default in `reviewable_output` mode
- **Reviewable output bank** created automatically unless `output_mode=in_place`
- **Curator controls** for policy mode, tool access, and freeform instructions
- **Transcript-aware curation** when transcript bundles are provided with
  `tool_access=operate`
- **Safe in-place promotion**: `in_place` runs always use an auto-generated
  staging bank, then archive originals and promote staged rows only after a
  successful commit
- **Terminal-state reviewability** so failed or canceled runs leave any staged
  output untouched for inspection and do not overwrite the source bank

## Quick start

```bash
# FORESIGHT_DB_URL must be set first — see "Set your database URL" above
FORESIGHT_DB_URL=$DATABASE_URL uv run foresight-server
uv run foresight --help
```

## Quick start (manual)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/daggerstuff/foresight.git
cd foresight
bash install.sh
```

Or if you already have the DSN:

```bash
FORESIGHT_DB_URL=$DATABASE_URL uv run foresight-server
uv run foresight --help
```

## Environment variables

All vars are documented in [`.env.example`](.env.example). The installer
generates `.env` from this template automatically.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `FORESIGHT_DB_URL` | **Yes** | — | PostgreSQL connection string |
| `FORESIGHT_IDENTITY` | No | `$USER` | User identity (`user` or `user@account`) |
| `FORESIGHT_BANK_ID` | No | `default` | Memory bank isolation |
| `FORESIGHT_LLM_PROVIDER` | No | — | LLM provider for synthesis/reflection |
| `FORESIGHT_LLM_API_KEY` | No | — | LLM API key |
| `FORESIGHT_LLM_BASE_URL` | No | — | LLM endpoint URL |
| `FORESIGHT_LLM_MODEL` | No | — | LLM model name |
| `FORESIGHT_REDIS_URL` | No | — | Redis companion cache (in-process cache if unset) |
| `FORESIGHT_ENABLE_WS` | No | — | Enable WebSocket subscriptions |
| `FORESIGHT_ALLOW_UNAUTHENTICATED` | No | — | Disable auth (local dev only) |
| `FORESIGHT_DECAY_INTERVAL_HOURS` | No | `6` | Decay sweep interval |
| `FORESIGHT_MAINTENANCE_INTERVAL_HOURS` | No | `24` | Maintenance + GC interval |

LLM, Redis, and WebSocket are all optional — the system works without them,
just with reduced features (no synthesis, in-process cache, stdio-only transport).

## systemd service

The installer auto-generates and installs a systemd user service from
[`foresight.service`](foresight.service). Manual setup:

```bash
cp foresight.service ~/.config/systemd/user/foresight-mcp.service
# Edit to replace __PROJECT_DIR__ and __UV_PATH__
systemctl --user daemon-reload
systemctl --user enable --now foresight-mcp
```

Check: `systemctl --user status foresight-mcp`  
Logs: `journalctl --user -u foresight-mcp -f`

## Add to your MCP client

### Claude Code

```json
{
  "mcpServers": {
    "foresight": {
      "command": "uv",
      "args": ["run", "-m", "foresight"],
      "cwd": "/path/to/foresight",
      "env": {
        "FORESIGHT_DB_URL": "postgresql://user:pass@host:5432/foresight",
        "FORESIGHT_IDENTITY": "username"
      }
    }
  }
}
```

### Goose

```yaml
extensions:
  foresight:
    args: ['run', '-m', 'foresight']
    cwd: /path/to/foresight
    env:
      FORESIGHT_DB_URL: postgresql://user:pass@host:5432/foresight
      FORESIGHT_IDENTITY: username
    type: stdio
```

### Other MCP clients

Use the same stdio pattern with `uv run -m foresight`.

### OpenCode — hands-off auto-inject

The installer (`install.sh`) auto-configures OpenCode: it patches
`opencode.json` to add the Foresight MCP server and copies the
`foresight-autoinject.js` plugin. Just restart OpenCode after install.

**Manual setup:**

Foresight's `inject_context` tool is designed to fire at conversation start
and on topic shifts, but MCP is request/response — the server can't push
context to the client. In practice this meant the agent had to *remember*
to call `inject_context` manually, which it rarely did.

The `foresight-autoinject` plugin fixes this. It uses OpenCode's
`experimental.chat.system.transform` hook to call `inject_context` via
MCP HTTP transport before every LLM request and append the result to the
system prompt — no agent action required.

**Setup:**

1. Run Foresight as an HTTP MCP server (Streamable HTTP transport):

```bash
export FORESIGHT_ENABLE_WS=1  # optional: WebSocket subscriptions
foresight-server --transport http --port 8764
```

2. Add to `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "foresight": {
      "type": "remote",
      "url": "http://127.0.0.1:8764/mcp",
      "enabled": true
    }
  },
  "plugin": [
    "./plugins/foresight-autoinject.js"
  ]
}
```

3. Copy [`foresight-autoinject.js`](../plugins/foresight-autoinject.js) to
   `~/.config/opencode/plugins/`.

4. Restart OpenCode. Context blocks and relevant memories now auto-inject
   into every system prompt — hands-off.

The plugin is non-fatal: if Foresight is down, it skips silently. Dedup
logic ensures it only injects once per new user message.

## Public surfaces

### Memory tools

These are the actual MCP tool names exposed by the server:

- `manage_memories` — store, update, delete, or archive a memory
- `search_memories` — unified search/retrieval (ID lookup, keyword, hybrid)
- `manage_context_blocks` — list, get, update, reset, or clear context blocks
- `process_session_transcript` — extract memories from a session transcript
- `manage_curation_runs` — create, get, list, cancel, or archive curation runs
- `inject_context` — surface relevant memories for a conversation
- `query_memories_temporal` — retrieve memories by time window or trend
- `get_system_status` — inspect health and memory-system status

Direct aliases, analysis/versioning, entity graph, clustering, embedding, decay,
document, tenant-switching, and maintenance routines are intentionally not
exposed as MCP tools. Use the `foresight` CLI or Python API for those workflows.

### Context block helpers

- `list_context_blocks`
- `get_context_block`
- `update_context_block`
- `add_context_guidance`
- `reset_context_block`
- `clear_context_block`
- `get_context_whisper`
- `get_context_snapshot`
- `manage_context_blocks`

### Curation workflow

- `manage_curation_runs`
- `ContextBlockAction`
- `CurationRunAction`
- CLI group: `foresight curate ...`

### Tool response contract

`manage_context_blocks` and `manage_curation_runs` return stable JSON envelopes:

```json
{
  "ok": true,
  "action": "get",
  "label": "guidance",
  "content": "Keep updates short and concrete."
}
```

Errors use the same envelope shape with `ok: false` and an `error.message`
field. The CLI `--json` mode prints these envelopes directly.

## Example usage

### Store a memory

```python
from foresight import store_memory

store_memory(
    content="User prefers short direct progress updates.",
    scope="session",
    retention="short_term",
    category="preference",
)
```

### Update continuity context

```python
from foresight import add_context_guidance, get_context_whisper

add_context_guidance("Keep updates short and concrete.", user_id="vivi")
whisper = get_context_whisper(user_id="vivi")
print(whisper)
```

### Create a reviewable curation run

```python
from foresight import CurationRunAction, manage_curation_runs

result = manage_curation_runs(
    CurationRunAction(
        action="create",
        source_bank_id="default",
        policy_mode="rebalance",
        tool_access="observe",
        output_mode="reviewable_output",
        instructions="Preserve durable preferences and merge duplicates.",
    ),
    user_id="vivi",
)
print(result)
```

### Run curation from the CLI

```bash
foresight curate create \
  --source-bank-id default \
  --policy-mode rebalance \
  --tool-access observe \
  --output-mode reviewable_output \
  --instructions "Preserve durable preferences and merge duplicates"
```

## Migration notes

Foresight now centers **context block** and **curation** terminology on the
public surface.

| Legacy name                 | Foresight-native name   |
| --------------------------- | ----------------------- |
| `manage_subconscious`       | `manage_context_blocks` |
| `get_subconscious_block`    | `get_context_block`     |
| `update_subconscious_block` | `update_context_block`  |
| `add_subconscious_guidance` | `add_context_guidance`  |
| `get_subconscious_whisper`  | `get_context_whisper`   |
| `get_subconscious_context`  | `get_context_snapshot`  |
| `reset_subconscious_block`  | `reset_context_block`   |
| `clear_subconscious_block`  | `clear_context_block`   |

Python compatibility helpers remain in place for older direct-import clients,
but new integrations should use the Foresight-native names above. These helpers
are not MCP-discovered tools.

## Evaluation harness (PIX-3953)

A reproducible end-to-end harness that measures **payload size**, **retrieval
quality**, **latency**, and **PII/secret safety** for `inject_context` and
`get_relevant_memories`. Seven seeded scenarios exercise preferences, pending
items, stale-vs-current facts, entity references, session recall, entity
salience, and decay priority.

### Run the harness

```bash
# Local run (text report on stdout)
foresight eval run

# Write a JSON report for CI / baseline diffs
foresight eval run --report eval-report.json --json
```

### Compare against a baseline

```bash
foresight eval run --save-baseline baseline.json -j
foresight eval run --compare baseline.json -r new.json
```

### Run the unit tests

```bash
uv run pytest tests/test_eval_harness.py -v
```

### Acceptance checks

- `7/7` scenarios pass with the default seed corpus.
- Avg injection payload stays under the configurable character budget.
- `scan_for_pii()` flags any leaked email, phone, SSN, API key, IP, or
  credit-card number in the formatted injection output.

## License

MIT
