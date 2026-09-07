# Installing Foresight

Foresight is an agentic persistent memory subsystem operating as a FastMCP server, CLI,
Textual TUI, and Python SDK. It persists semantic context into PostgreSQL 17 + `pgvector`
(e.g. Neon) with AES-256-GCM envelope encryption and optional Redis companion caching.

---

## ⚡ Option 1: One-Liner (Recommended)

To install or upgrade on a machine:

```bash
curl -fsSL https://raw.githubusercontent.com/daggerstuff/foresight/main/install.sh | bash
```

Or, from inside a cloned repository:

```bash
bash install.sh
```

### What the installer handles automatically

1. **Prerequisites Verification**: Validates or installs `uv` and verifies Python 3.12+
   (auto-provisions via `uv` if needed).
2. **Environment Isolation**: Isolates Foresight from ambient virtualenvs (unsets outer
   `VIRTUAL_ENV`) to prevent dependency shadowing.
3. **Full Package Installation**: Runs `uv sync --extra all` to install FastMCP 4.0,
   psycopg 3.3+ PostgreSQL drivers with pooling, Redis cache, cryptography, CLI (`typer`,
   `rich`), and TUI (`textual`).
4. **Database Provisioning**: Walks through PostgreSQL setup (Neon with `sslmode=require`,
   Supabase, Railway, or local) and validates connectivity before saving `.env`.
5. **Secure Configuration**: Stores credentials in `.env` with restrictive `chmod 600`
   permissions.
6. **Schema Initialization**: Creates all 23 database tables, indexes, and pgvector
   embeddings in the public schema.
7. **Diagnostics**: Runs an 11-point health check (`foresight doctor`).
8. **Daemon Service**: Installs and starts `foresight.service` under the user's systemd
   session on `http://127.0.0.1:8764/mcp`, enabling lingering across disconnects.
9. **Agent Integrations**: Automatically registers Foresight into **Claude Code**
   (`~/.claude.json`), **OpenCode** (`opencode.jsonc` + autoinject plugin), and links CLI
   to `~/.local/bin`.
10. **Onboarding Verification**: Stores a verification memory to ensure write and read
    paths work end-to-end.

> **Postgres is required.** SQLite is not supported for production. If `FORESIGHT_DB_URL`
> is not already exported, the installer guides you to connect Neon, Supabase, Railway,
> or local PostgreSQL.

---

## 🛠️ Option 2: Manual Setup / Submodule Development

When working inside a monorepo (e.g. `pixelated`) or cloning directly:

```bash
# 1. Clone repository or initialize submodule
git clone https://github.com/daggerstuff/foresight.git
cd foresight

# 2. Isolate environment and install all packages
unset VIRTUAL_ENV
uv sync --extra all

# 3. Configure environment
cp .env.example .env
chmod 600 .env

# Edit .env with your Postgres DSN:
# FORESIGHT_DB_URL="postgresql://user:pass@ep-host.region.neon.tech/foresight?sslmode=require"
# FORESIGHT_IDENTITY="your-username"
# FORESIGHT_BANK_ID="default"

# 4. Initialize schema and verify health
uv run foresight init --force
uv run foresight doctor

# 5. Link CLI to PATH
mkdir -p ~/.local/bin
ln -sf "$PWD/scripts/foresight" ~/.local/bin/foresight
```

For development and testing:

```bash
uv sync --extra all --dev
uv run pytest                     # run pytest test suite
uv run ruff check .               # lint checks
uv run foresight prove            # run 9-point proof benchmark suite
uv run foresight-server           # start MCP server directly
```

---

## 📦 Option 3: Install from PyPI

```bash
uv pip install "foresight[all]"
# or: pip install "foresight[all]"

# Set connection string and initialize
export FORESIGHT_DB_URL='postgresql://user:pass@ep-host.neon.tech/foresight?sslmode=require'
export FORESIGHT_IDENTITY='username'
foresight init
foresight doctor
```

### Distribution Extras Breakdown

| Extra    | Includes                                             | Primary Use Case                          |
| -------- | ---------------------------------------------------- | ----------------------------------------- |
| `(none)` | FastMCP server, psycopg 3.3+, Redis, cryptography    | Headless MCP server / background daemon   |
| `[cli]`  | Base + Typer & Rich CLI                              | Terminal management and scripted tools    |
| `[tui]`  | Base + Textual interactive terminal UI               | Fullscreen interactive memory browser     |
| `[redis]`| Base + Redis cache driver                            | Explicit companion cache dependency       |
| `[all]`  | FastMCP + CLI + TUI + Redis + PostgreSQL + Crypto    | Complete installation (recommended)       |

---

## 🤖 Connecting AI Agent Tooling

Foresight exposes an 8-tool FastMCP surface:
- `inject_context`, `manage_memories`, `search_memories`, `manage_context_blocks`
- `query_memories_temporal`, `manage_encryption`, `process_session_transcript`, `get_system_status`

### 1. Claude Code

#### Option A: Streamable HTTP Daemon (Recommended)

When `foresight.service` is running on port 8764:

```bash
claude mcp add --transport http foresight http://127.0.0.1:8764/mcp
```

Or configure directly in `~/.claude.json`:

```json
{
  "mcpServers": {
    "foresight": {
      "url": "http://127.0.0.1:8764/mcp"
    }
  }
}
```

#### Option B: Local Stdio Transport

```json
{
  "mcpServers": {
    "foresight": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/foresight", "--no-active", "foresight-server"],
      "env": {
        "FORESIGHT_DB_URL": "postgresql://user:pass@host:5432/foresight?sslmode=require",
        "FORESIGHT_IDENTITY": "your-username",
        "FORESIGHT_ALLOW_UNAUTHENTICATED": "1"
      }
    }
  }
}
```

### 2. OpenCode

Add to `~/.config/opencode/opencode.jsonc` (or `opencode.json`):

```json
{
  "plugin": [
    "./plugins/foresight-autoinject.js"
  ],
  "mcp": {
    "foresight": {
      "type": "remote",
      "url": "http://127.0.0.1:8764/mcp",
      "enabled": true
    }
  }
}
```

To enable Turn 1 auto-injection, copy the plugin:

```bash
mkdir -p ~/.config/opencode/plugins
cp plugins/foresight-autoinject.js ~/.config/opencode/plugins/
```

### 3. Google Antigravity & Gemini CLI

Antigravity natively loads lazy MCP tools from the server config:

```json
{
  "mcpServers": {
    "foresight": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/foresight", "--no-active", "foresight-server"],
      "env": {
        "FORESIGHT_DB_URL": "postgresql://user:pass@ep-host.region.neon.tech/foresight?sslmode=require",
        "FORESIGHT_IDENTITY": "your-username",
        "FORESIGHT_BANK_ID": "default",
        "FORESIGHT_ALLOW_UNAUTHENTICATED": "1"
      }
    }
  }
}
```

In chat sessions, Antigravity calls Foresight seamlessly via `call_mcp_tool`:

```python
call_mcp_tool(
    ServerName="foresight",
    ToolName="inject_context",
    Arguments={"conversation_text": "current topic..."},
)
```

---

## 🩺 Verifying the Installation

Run the diagnostics suite:

```bash
foresight doctor
```

Expected output:

```text
Foresight Diagnostics

  ✓ Python 3.11+
  ✓ Config dir exists
  ✓ Config file exists
  ✓ Database URL configured
  ✓ User ID configured
  ✓ Bank ID configured
  ✓ Database responsive
  ✓ LLM provider configured
  ✓ Redis cache
  ✓ MCP HTTP server
  ✓ Schema version

All 11 checks passed
```

### Quick Verification Commands

```bash
# Check system status and memory distribution
foresight status

# Store a test memory
foresight store "Testing persistence pipeline" --scope project

# Query memories
foresight query "persistence"

# Launch the interactive terminal UI
foresight tui

# Verify AES-256-GCM encryption status
foresight security status

# Run the 9-point production benchmark
foresight prove
```
