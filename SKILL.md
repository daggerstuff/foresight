# Foresight — Agent Skill

Persistent memory for AI agents. This file tells you how to set up Foresight,
use every tool correctly, and apply best practices — including how to inject
context automatically without the user having to ask.

---

## 1. Quick orientation

Foresight gives you a **persistent, queryable memory store** backed by
PostgreSQL. It exposes eight MCP tools, a CLI, and a TUI. As an agent your
primary surface is the MCP server; the CLI is useful for bootstrapping and
health-checks.

Two concepts underpin everything:

| Concept | What it is |
|---|---|
| **Memories** | Discrete facts, preferences, events, and observations, each with a scope, retention policy, category, and importance score |
| **Context blocks** | Named, free-text scratchpads (`user_preferences`, `pending_items`, `session_patterns`) that are injected alongside memories during retrieval |

---

## 2. Check whether Foresight is already running

Before any setup work, check:

```bash
foresight doctor          # runs 7-point health check; exit 0 = healthy
get_system_status         # MCP tool — returns JSON health report
```

If `foresight` is not found or `doctor` fails, proceed to section 3.

---

## 3. Self-install (no user action required for the common path)

### 3a. Server only — fastest path (uv required)

If the user has `uv` installed (`which uv`), you can run the server directly
from PyPI without a separate install step:

```bash
FORESIGHT_DB_URL="<dsn>" FORESIGHT_IDENTITY="<name>" \
  uvx foresight-server
```

The server calls `init_db()` on startup — no separate `foresight init` needed.

### 3b. Full install (CLI + TUI + server)

```bash
pip install "foresight[all]"          # or: uv pip install "foresight[all]"

export FORESIGHT_DB_URL="postgresql://user:pass@host:5432/db?sslmode=require"
export FORESIGHT_IDENTITY="alice"     # or "alice:acme-corp" — see section 4

foresight init      # creates ~/.foresight/config.json, runs schema migrations
foresight doctor    # verify all 7 checks pass before proceeding
```

### 3c. Install check script (run this first, then branch)

```bash
if command -v foresight &>/dev/null && foresight doctor --quiet 2>/dev/null; then
  echo "already healthy — skip install"
elif command -v uv &>/dev/null; then
  echo "use uvx path"
else
  pip install "foresight[all]"
  foresight init
fi
```

### Database requirement

Foresight is **Postgres-only**. You need a `FORESIGHT_DB_URL` DSN before
anything works. Common sources:

| Environment | How to get the DSN |
|---|---|
| Replit | `$DATABASE_URL` is injected automatically — pass it as `FORESIGHT_DB_URL=$DATABASE_URL` |
| Supabase / Neon / Railway | Copy the connection string from the dashboard |
| Local Postgres | `postgresql://postgres:password@localhost:5432/foresight` |

---

## 4. Environment variables

| Variable | Required | Description |
|---|---|---|
| `FORESIGHT_DB_URL` | **Yes** | Postgres DSN — `postgresql://user:pass@host:port/db` |
| `FORESIGHT_IDENTITY` | Recommended | Identity string. Plain `alice` → user_id=alice. Compound `alice:acme-corp` → user_id=alice, account_id=acme-corp. Account-ID groups memories across a workspace/org. |
| `FORESIGHT_BANK_ID` | No | Memory bank namespace (default: derived from identity). Use to separate memory pools, e.g. `work` vs `personal`. |
| `FORESIGHT_HOST` | No | Bind host for the HTTP server (default: `127.0.0.1`). Set `0.0.0.0` to accept external connections. |
| `FORESIGHT_PORT` | No | Bind port (default: `8000`). |

---

## 5. Add to an MCP client

### Claude Code (`~/.claude/settings.json` or project `.mcp.json`)

```json
{
  "mcpServers": {
    "foresight": {
      "command": "uvx",
      "args": ["foresight-server"],
      "env": {
        "FORESIGHT_DB_URL": "postgresql://...",
        "FORESIGHT_IDENTITY": "alice"
      }
    }
  }
}
```

If running from a local clone:

```json
{
  "mcpServers": {
    "foresight": {
      "command": "uv",
      "args": ["run", "foresight-server"],
      "env": {
        "FORESIGHT_DB_URL": "postgresql://...",
        "FORESIGHT_IDENTITY": "alice"
      }
    }
  }
}
```

### Cursor (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "foresight": {
      "command": "uvx",
      "args": ["foresight-server"],
      "env": {
        "FORESIGHT_DB_URL": "postgresql://...",
        "FORESIGHT_IDENTITY": "alice"
      }
    }
  }
}
```

### Goose (`~/.config/goose/config.yaml`)

```yaml
extensions:
  foresight:
    type: stdio
    command: uvx
    args: [foresight-server]
    envs:
      FORESIGHT_DB_URL: "postgresql://..."
      FORESIGHT_IDENTITY: "alice"
```

---

## 6. Auto-injection (the subconscious feature)

Foresight registers an MCP prompt called **`foresight_autocontext`**. When a
client includes it in the system message, you will automatically call
`inject_context` at conversation start, on topic shifts, and on recall-signal
phrases — no user prompt needed.

**Enable it in Claude Code** — run once per project:

```
/use foresight_autocontext
```

This appends the prompt to the project's system context. From that point on,
you call `inject_context` on your own every session.

**What the prompt instructs you to do:**

1. Before your first reply, call `inject_context` with the user's opening message.
2. On topic shift (new subject, project, person, task), call it again.
3. On recall signals ("last time", "as we discussed", "you mentioned"), call immediately.
4. Before non-trivial actions (code, plans, advice), call first.
5. Silently apply what comes back — never announce the call.

If the client does not support server prompts, reproduce this behaviour
manually by calling `inject_context` at the start of every session.

---

## 7. Tool reference

### `inject_context` — retrieve relevant memories for the current conversation

```
inject_context(
  conversation_text = "<current message or conversation>",
  max_memories      = 5,        # optional, default 5
  min_relevance     = 0.3,      # optional, default 0.3
  max_chars         = 4000,     # optional; budgeted output when set
  include_details   = False,    # True → JSON with structured memories + budget
)
```

**When to call:** see section 6. The tool is cheap — call it freely; it returns
nothing if nothing is relevant.

**Output:** formatted string of memories and context block signals. When
`include_details=True`, returns JSON with keys `formatted`, `memories`,
`context_blocks`, and (if `max_chars` set) `budget`.

---

### `manage_memories` — store, update, delete, archive

```
# Store
manage_memories(
  action  = "store",
  content = "User prefers concise answers with code examples",
  store_options = {
    "category":   "preference",   # fact | preference | event | observation | pending | pattern
    "scope":      "arc",          # session | arc | trait | fact
    "retention":  "long_term",    # ephemeral | short_term | long_term | permanent
    "importance": 0.8,            # 0.0–1.0
  }
)

# Update
manage_memories(action="update", memory_id="<id>", updates={"content": "..."})

# Delete / archive
manage_memories(action="delete",  memory_id="<id>")
manage_memories(action="archive", memory_id="<id>")
```

**Scope guide:**

| Scope | Meaning |
|---|---|
| `session` | Relevant only for this conversation |
| `arc` | Project- or task-level; persists across sessions |
| `trait` | Stable personal trait or long-running preference |
| `fact` | Objective fact with no expiry expectation |

**Retention guide:**

| Retention | Meaning |
|---|---|
| `ephemeral` | Deleted after current session |
| `short_term` | Kept for days; subject to decay |
| `long_term` | Kept indefinitely unless manually removed |
| `permanent` | Never decayed or archived automatically |

**What to store** — store things the user would be frustrated to repeat. Good
candidates: stated preferences, project decisions, open tasks, key facts about
people/projects, and anything the user says to "remember".

---

### `search_memories` — look up memories by content, ID, or list

```
# Keyword / hybrid search
search_memories(query_type="keyword", query="postgres migration", limit=10)

# Hybrid (keyword + TF-IDF + graph + temporal)
search_memories(query_type="keyword", query="...", use_hybrid=True)

# List all
search_memories(query_type="list", limit=20, offset=0)

# Fetch by ID
search_memories(query_type="id", memory_id="<id>")
```

Use `use_cascade=True` to follow graph relationships (related memories pulled
in automatically).

---

### `manage_context_blocks` — read and write named scratchpads

Context blocks are always-on named text blocks that `inject_context` includes
alongside memories. There are three built-in blocks:

| Label | Purpose |
|---|---|
| `user_preferences` | Behavioural preferences — tone, format, constraints |
| `pending_items` | Open tasks, follow-ups, things to do |
| `session_patterns` | Recurring patterns observed across sessions |

```
# List all blocks
manage_context_blocks(action="list")

# Read one block
manage_context_blocks(action="get", label="user_preferences")

# Write / append
manage_context_blocks(
  action  = "update",
  label   = "user_preferences",
  content = "Prefers TypeScript over JavaScript. Wants tests for every function."
)

# Clear a block (empty it)
manage_context_blocks(action="clear", label="pending_items")

# Reset to default content
manage_context_blocks(action="reset", label="session_patterns")
```

**When to update context blocks:**

- After the user states a preference → append to `user_preferences`
- When a task is mentioned but not finished → add to `pending_items`
- After a session ends → summarise what happened in `session_patterns`
- Do **not** put ephemeral conversation detail into blocks — use memories for
  that; blocks are always injected and consume budget.

---

### `process_session_transcript` — extract memories from a full conversation

Call this at the end of a session to extract and persist what was learned.
Runs the capture pipeline, bridges context blocks into memories, and extracts
entities into the graph store.

```
process_session_transcript(
  session_id   = "session-2026-07-23-alice",
  messages     = [
    {"role": "user",      "content": "..."},
    {"role": "assistant", "content": "..."},
  ],
  project_path = "/path/to/project",   # optional
)
```

Best practice: call this after significant sessions (not trivial chitchat).
Use a stable, human-readable `session_id` (date + identity works well).

---

### `query_memories_temporal` — fetch memories by time window or trend

```
# Memories from this week
query_memories_temporal(window="week", limit=20)

# Memories from today in a specific category
query_memories_temporal(window="today", category="preference")

# Memories by trend (rising / stable / stale)
query_memories_temporal(trend="rising", limit=10)
```

Useful for surfacing recently added or frequently activated memories without
a keyword query.

---

### `manage_curation_runs` — async memory quality jobs

Curation runs deduplicate, consolidate, detect contradictions, and synthesise
cross-memory insights. High-impact changes are staged for review, never
auto-applied.

```
# Start a curation run
manage_curation_runs(
  action      = "create",
  policy_mode = "preserve",    # preserve | rebalance | rebuild
  tool_access = "observe",     # observe | operate
)

# Check status
manage_curation_runs(action="get", run_id="<id>")

# List recent runs
manage_curation_runs(action="list")

# Cancel
manage_curation_runs(action="cancel", run_id="<id>")
```

Run curation occasionally (not every session). `preserve` mode is the
safest — it only flags issues, never modifies. Use `rebalance` when the
memory store has grown stale or noisy.

---

### `get_system_status` — health check and statistics

```
get_system_status()

# With trends
get_system_status(include_trends=True, timeframe="30 days")
```

Returns memory count by scope, last capture time, maintenance stats, and
(optionally) TF-IDF cache metrics. Call this when diagnosing issues or to
confirm the server is healthy.

---

## 8. Best practices

### Do

- **Call `inject_context` before replying** to any non-trivial message — cost
  is negligible, benefit is high.
- **Store at the right scope.** Fleeting observations → `session`. Decisions
  that span a project → `arc`. Long-standing traits → `trait` or `permanent`.
- **Update `user_preferences` immediately** when the user states a preference,
  so it is available in future sessions without them repeating it.
- **Maintain `pending_items`** — add items when tasks come up, remove or update
  them when done. This block is always injected, so stale items waste budget.
- **Process transcripts after significant sessions** using
  `process_session_transcript` — it does richer extraction than manual
  `manage_memories` calls.
- **Set `FORESIGHT_IDENTITY`** per user. Without it, all memories land in the
  same anonymous bucket and cannot be separated later.

### Don't

- Don't store the same fact twice. `inject_context` deduplicates on retrieval
  but the store grows — use `manage_curation_runs` periodically to clean up.
- Don't put large blobs in context blocks. Keep each block focused: one
  preference per line, one task per line. Blocks are always injected and there
  is a character budget.
- Don't use `scope=session` for anything the user might reference next week.
  When in doubt, use `arc`.
- Don't announce that you called `inject_context` or `manage_memories` unless
  the user asks. Memory operations should be invisible.
- Don't run curation with `tool_access=operate` and `output_mode=in_place`
  without understanding that it modifies memories in-place. Use `observe` first.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `FORESIGHT_DB_URL` not set | Export it before starting the server, or pass inline: `FORESIGHT_DB_URL=... uvx foresight-server` |
| `foresight doctor` shows DB failure | Check the DSN; confirm the Postgres instance is reachable: `psql "$FORESIGHT_DB_URL" -c '\l'` |
| Server starts but tools return empty | Check `FORESIGHT_IDENTITY` — without it user_id defaults to empty and queries miss all stored memories |
| MCP client can't connect | Confirm the server is running on the expected host/port; check `FORESIGHT_HOST=0.0.0.0` if connecting from a remote client |
| `inject_context` returns nothing | The store may be empty. Run `search_memories(query_type="list")` to verify. If empty, start storing. |
| Port already in use | The server is already running. Kill the old process (`pkill -f foresight-server`) or change `FORESIGHT_PORT`. |
