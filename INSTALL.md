# Installing Foresight MCP

## Option 1: Install from PyPI (recommended)

```bash
# Install with pip
pip install foresight

# Or with uv
uv add foresight
```

## Option 2: Run directly from repo

```bash
# Clone the repo
git clone https://github.com/your-org/foresight.git
cd foresight

# Install with uv — include the postgres driver
uv sync --extra all

# Set your Postgres DSN (required — SQLite is no longer supported)
export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'

# Run the server
uv run foresight-server
```

## Option 3: Development mode

```bash
git clone https://github.com/your-org/foresight.git
cd foresight

# Install in editable mode with all extras (includes postgres driver)
uv sync --extra all --dev

# Set your Postgres DSN
export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'

# Run tests
uv run pytest

# Run server
uv run foresight-server
```

## Add to Claude Code

After installation, add to your `~/.claude/settings.json` or project's
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

> **Note:** `FORESIGHT_DB_URL` is required. `FORESIGHT_IDENTITY` sets the active
> user identity (previously `FORESIGHT_USER_ID` — that name is deprecated).

## Verify installation

```bash
# Check version
uv run foresight --version

# Run diagnostics (7-point health check)
uv run foresight system doctor
```
