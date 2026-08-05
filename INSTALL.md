# Installing Foresight

## One-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/daggerstuff/foresight/master/install.sh | bash
```

Or, from a cloned repo:

```bash
bash install.sh
```

The installer will:

- Check prerequisites (Python 3.12+, uv)
- Install all dependencies (CLI + TUI + MCP server + Postgres driver)
- Walk you through connecting a Postgres database if one isn't already
  configured
- Initialise config and run a 7-point health check

> **Postgres is required.** SQLite is not supported. The installer will prompt
> you to choose a provider (Neon, Supabase, Railway, Replit, local, or bring
> your own DSN) if `FORESIGHT_DB_URL` is not already set.

---

## Option 2 — Install from PyPI

```bash
pip install foresight[all]
# then set your DSN and init:
export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'
foresight system init
foresight system doctor
```

Extras breakdown:

| Extra    | Includes                            |
| -------- | ----------------------------------- |
| `(none)` | MCP server only — no CLI, no TUI    |
| `[cli]`  | CLI (`typer` + `rich`) — no TUI     |
| `[tui]`  | CLI + TUI (`textual`) — no MCP      |
| `[all]`  | Everything — CLI + TUI + MCP server |

---

## Option 3 — Development mode

```bash
git clone https://github.com/daggerstuff/foresight.git
cd foresight

uv sync --extra all --dev

export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'

uv run pytest          # run the test suite
uv run foresight-server  # start the MCP server
```

---

## Add to Claude Code

After installation, add to `~/.claude/settings.json` or your project's
`.mcp.json`:

```json
{
  "mcpServers": {
    "foresight": {
      "command": "uv",
      "args": ["run", "foresight-server"],
      "env": {
        "FORESIGHT_DB_URL": "postgresql://user:pass@host:5432/db?sslmode=require",
        "FORESIGHT_IDENTITY": "username"
      }
    }
  }
}
```

> `FORESIGHT_DB_URL` is required. `FORESIGHT_IDENTITY` sets the active user
> identity (`FORESIGHT_USER_ID` is deprecated).

---

## Verify installation

```bash
uv run foresight --version
uv run foresight system doctor    # 7-point health check
```
