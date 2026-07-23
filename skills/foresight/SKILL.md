---
name: foresight
description: >
  Persistent memory for AI agents via the Foresight MCP server. Use this skill
  whenever Foresight is (or should be) connected as an MCP server — it teaches
  you how to self-install and configure Foresight if it isn't running yet, how
  to call every tool correctly, and how to apply memory best practices. Activate
  when the user mentions Foresight, when you detect foresight MCP tools in your
  tool list (inject_context, manage_memories, search_memories, etc.), or when
  the user asks you to remember something across sessions.
license: MIT
metadata:
  author: daggerstuff
  version: "1.0.0"
---

# Foresight

Persistent memory for AI agents. This skill covers self-install, MCP client
configuration, auto-injection, every tool, and best practices.

## When to activate

- Foresight MCP tools appear in your tool list (`inject_context`,
  `manage_memories`, `search_memories`, etc.)
- The user mentions "Foresight" or asks you to remember something across
  sessions
- The user asks you to set up or configure Foresight

---

## 1. Check if Foresight is already running

Before any setup work:

```bash
foresight doctor          # 7-point health check; exit 0 = healthy
```

Or call the `get_system_status` MCP tool — a JSON response means the server is
up. If either succeeds, skip to section 5 (auto-injection).

---

## 2. Self-install (minimal user action required)

### Fastest path — uv installed

If `uv` is available (`which uv`), run the server directly from PyPI — no
separate install or init step needed (the server runs `init_db()` on startup):

```bash
FORESIGHT_DB_URL="<dsn>" FORESIGHT_IDENTITY="<name>" uvx foresight-server
```

### Full install (CLI + TUI + server)

```bash
pip install "foresight[all]"   # or: uv pip install "foresight[all]"

export FORESIGHT_DB_URL="postgresql://user:pass@host:5432/db?sslmode=require"
export FORESIGHT_IDENTITY="alice"   # see section 3 for format

foresight init     # creates ~/.foresight/config.json; runs schema migrations
foresight doctor   # verify all 7 checks pass
```

### Decision script — run this, then branch

```bash
if command -v foresight &>/dev/null && foresight doctor --quiet 2>/dev/null; then
  : # already healthy
elif command -v uv &>/dev/null; then
  FORESIGHT_DB_URL="$FORESIGHT_DB_URL" FORESIGHT_IDENTITY="$FORESIGHT_IDENTITY" \
    uvx foresight-server &
else
  pip install "foresight[all]" && foresight init
fi
```

### Database requirement

Foresight is **Postgres-only**. `FORESIGHT_DB_URL` must be set before anything
works.

| Environment | Source of DSN |
|---|---|
| Replit | `$DATABASE_URL` is injected automatically — pass as `FORESIGHT_DB_URL=$DATABASE_URL` |
| Supabase / Neon / Railway | Copy the connection string from the project dashboard |
| Local Postgres | `postgresql://postgres:password@localhost:5432/foresight` |

---

## 3. Environment variables

| Variable | Required | Description |
|---|---|---|
| `FORESIGHT_DB_URL` | **Yes** | Postgres DSN |
| `FORESIGHT_IDENTITY` | Recommended | `alice` → user_id=alice. `alice:acme-corp` → user_id=alice, account_id=acme-corp (groups memories across a workspace) |
| `FORESIGHT_BANK_ID` | No | Memory bank namespace — separate pools, e.g. `work` vs `personal` |
| `FORESIGHT_HOST` | No | Bind host (default `127.0.0.1`; set `0.0.0.0` for external access) |
| `FORESIGHT_PORT` | No | Bind port (default `8000`) |

---

## 4. Add to an MCP client

### Claude Code

`~/.claude/settings.json` or project `.mcp.json`:

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

Then enable auto-injection (once per project):

```
/use foresight_autocontext
```

### Cursor — `.cursor/mcp.json`

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

### Goose — `~/.config/goose/config.yaml`

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

### From a local clone (any client)

Replace `uvx` / `args: ["foresight-server"]` with:

```json
"command": "uv",
"args": ["run", "foresight-server"]
```

---

## 5. Auto-injection — call inject_context automatically

The server registers an MCP prompt called **`foresight_autocontext`**. When a
client includes it in the system message, you call `inject_context` on your own
at conversation start, on topic shifts, and on recall signals — no user prompt
needed.

**Enable in Claude Code** (run once per project):

```
/use foresight_autocontext
```

**What you must do when this prompt is active — follow without being asked:**

1. **Conversation start** — before your first reply, call `inject_context` with
   the user's opening message. Read what comes back and let it inform your
   response silently.
2. **Topic shift** — new subject, project, person, or task → call again before
   replying.
3. **Recall signals** — "last time", "as we discussed", "you mentioned", "remember
   when" → call immediately.
4. **Before non-trivial actions** — code, plans, advice → call first.
5. **Never announce the call.** Apply what you find invisibly. If nothing relevant
   comes back, proceed normally.

If the client does not support server prompts, reproduce this behaviour manually
by calling `inject_context` at the start of every session.

---

## 6. Tool reference

### `inject_context` — retrieve relevant memories for the current conversation

```
inject_context(
  conversation_text = "<current message or recent conversation>",
  max_memories      = 5,      # default 5
  min_relevance     = 0.3,    # default 0.3; lower = more results
  max_chars         = 4000,   # set to budget output size
  include_details   = False,  # True → JSON with structured memories + budget breakdown
)
```

Returns a formatted string of relevant memories and context block signals.
Returns a no-results message (not an error) when nothing is relevant — this is
normal and safe to ignore.

---

### `manage_memories` — store, update, delete, archive

```python
# Store
manage_memories(
  action  = "store",
  content = "User prefers TypeScript and wants tests for every function.",
  store_options = {
    "category":   "preference",  # fact | preference | event | observation | pending | pattern
    "scope":      "trait",       # session | arc | trait | fact
    "retention":  "long_term",   # ephemeral | short_term | long_term | permanent
    "importance": 0.8,           # 0.0–1.0
  }
)

# Update
manage_memories(action="update", memory_id="<id>", updates={"content": "..."})

# Delete or archive
manage_memories(action="delete",  memory_id="<id>")
manage_memories(action="archive", memory_id="<id>")
```

**Scope guide:**

| Scope | Use for |
|---|---|
| `session` | Relevant only to this conversation |
| `arc` | Project- or task-level; persists across sessions |
| `trait` | Stable personal trait or long-running preference |
| `fact` | Objective fact with no expiry expectation |

**Retention guide:**

| Retention | Lifetime |
|---|---|
| `ephemeral` | Deleted after current session |
| `short_term` | Days; subject to decay |
| `long_term` | Indefinite unless manually removed |
| `permanent` | Never decayed or auto-archived |

---

### `search_memories` — look up memories by content, ID, or full list

```python
# Keyword / hybrid search (recommended for most lookups)
search_memories(query_type="keyword", query="postgres migration", limit=10)

# Hybrid with graph traversal
search_memories(query_type="keyword", query="...", use_hybrid=True, use_cascade=True)

# List all memories
search_memories(query_type="list", limit=20, offset=0)

# Fetch one by ID
search_memories(query_type="id", memory_id="<id>")
```

---

### `manage_context_blocks` — named always-on scratchpads

Three built-in blocks are always injected alongside memories during retrieval:

| Label | Purpose |
|---|---|
| `user_preferences` | Tone, format, constraints, coding style |
| `pending_items` | Open tasks and follow-ups |
| `session_patterns` | Recurring patterns observed across sessions |

```python
manage_context_blocks(action="list")                              # show all blocks
manage_context_blocks(action="get",    label="user_preferences")  # read one
manage_context_blocks(action="update", label="user_preferences",  # write
                      content="Prefers concise answers. TypeScript only.")
manage_context_blocks(action="clear",  label="pending_items")     # empty it
manage_context_blocks(action="reset",  label="session_patterns")  # restore default
```

**When to update:**
- User states a preference → append to `user_preferences`
- Task mentioned but not finished → add to `pending_items`
- Session ends → summarise key patterns in `session_patterns`

---

### `process_session_transcript` — extract memories from a full conversation

Call at the end of significant sessions. Runs the full capture pipeline,
bridges context blocks into memories, and extracts entities into the graph.

```python
process_session_transcript(
  session_id   = "2026-07-23-alice",       # stable, human-readable ID
  messages     = [
    {"role": "user",      "content": "..."},
    {"role": "assistant", "content": "..."},
  ],
  project_path = "/path/to/project",        # optional; improves extraction
)
```

Prefer this over manual `manage_memories` calls — it extracts more and deduplicates.

---

### `query_memories_temporal` — fetch by time window or trend

```python
query_memories_temporal(window="week",  limit=20)              # this week
query_memories_temporal(window="today", category="preference") # today, filtered
query_memories_temporal(trend="rising", limit=10)              # gaining activation
```

---

### `manage_curation_runs` — async memory quality jobs

Deduplication, contradiction detection, stale archival, cross-memory synthesis.
High-impact changes are staged for review, never auto-applied.

```python
# Start (use 'observe' first — safest)
manage_curation_runs(action="create", policy_mode="preserve", tool_access="observe")

manage_curation_runs(action="get",    run_id="<id>")   # check status
manage_curation_runs(action="list")                    # recent runs
manage_curation_runs(action="cancel", run_id="<id>")   # cancel
```

Run occasionally, not every session. `preserve` + `observe` is always safe.
Use `rebalance` when the store has grown noisy. Never use `in_place` mode
without understanding it modifies memories permanently.

---

### `get_system_status` — health and statistics

```python
get_system_status()
get_system_status(include_trends=True, timeframe="30 days")
```

Returns memory count by scope, last capture time, maintenance stats, cache
metrics. Use to diagnose issues or confirm the server is healthy.

---

## 7. Best practices

**Do:**
- Call `inject_context` before any non-trivial reply — cost is low, benefit is high.
- Store at the right scope. Fleeting observation → `session`. Project decision → `arc`. Personal trait → `trait` or `permanent`.
- Update `user_preferences` immediately when the user states a preference — don't wait for end of session.
- Keep `pending_items` current. Add items when they come up; remove or tick them off when done. Stale items waste injection budget every session.
- Use `process_session_transcript` at the end of meaningful sessions.
- Set `FORESIGHT_IDENTITY` per user — without it all memories land in the same anonymous bucket and cannot be separated later.

**Don't:**
- Announce memory operations. Storing and retrieving memories should be invisible.
- Put large blobs in context blocks. One preference per line, one task per line. Blocks are always injected.
- Use `scope=session` for anything the user might reference next week. When in doubt, use `arc`.
- Store the same fact twice — run curation periodically to clean up.
- Use `tool_access=operate` + `output_mode=in_place` curation without understanding it modifies in place. Always start with `observe`.

---

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| Server won't start — `FORESIGHT_DB_URL` missing | Export it or pass inline: `FORESIGHT_DB_URL=... uvx foresight-server` |
| `foresight doctor` shows DB failure | Verify DSN: `psql "$FORESIGHT_DB_URL" -c '\l'` |
| Tools return empty results | Check `FORESIGHT_IDENTITY` — without it, user_id is empty and all queries miss. Run `search_memories(query_type="list")` to confirm store has data. |
| MCP client can't connect | Confirm `FORESIGHT_HOST=0.0.0.0` when connecting from a remote client; check port matches `FORESIGHT_PORT`. |
| Port already in use | Kill stale process: `pkill -f foresight-server`. Or change `FORESIGHT_PORT`. |
| `inject_context` always returns no results | Store is empty. Start with `manage_memories(action="store", ...)` or run `process_session_transcript`. |
