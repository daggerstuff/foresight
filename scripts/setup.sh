#!/usr/bin/env bash

# Exit on any error
set -e

echo "Setting up Foresight MCP..."

# Ensure uv is installed (skip if already present)
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found, installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Install Python dependencies with all extras (CLI + TUI + Postgres driver)
uv sync --extra all

# Create symlink for one-liner access (optional)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -d "$HOME/.local/bin" ]; then
  if [ ! -f "$HOME/.local/bin/foresight" ]; then
    ln -sf "$SCRIPT_DIR/foresight" "$HOME/.local/bin/foresight"
    echo "  → Created symlink: ~/.local/bin/foresight"
  fi
fi

# Create memory directory with secure permissions
MEMORY_DIR="$HOME/.foresight"
mkdir -p "$MEMORY_DIR"
chmod 700 "$MEMORY_DIR"

# Resolve the Postgres DSN.
# Prefer FORESIGHT_DB_URL if already set; fall back to DATABASE_URL (Replit's
# managed Postgres). Without a DSN the server cannot start — SQLite is no
# longer supported.
if [ -z "${FORESIGHT_DB_URL:-}" ]; then
  if [ -n "${DATABASE_URL:-}" ]; then
    export FORESIGHT_DB_URL="$DATABASE_URL"
    echo "  → Using DATABASE_URL as FORESIGHT_DB_URL"
  else
    echo ""
    echo "⚠️  FORESIGHT_DB_URL is not set."
    echo "   Foresight requires a PostgreSQL database. Set the environment variable"
    echo "   before running 'foresight init' or starting the MCP server:"
    echo ""
    echo "     export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'"
    echo ""
    echo "   On Replit, DATABASE_URL is provided automatically — no manual step needed."
    echo "   Skipping database initialization. Run 'foresight init' once the DSN is set."
    echo ""
    echo "Setup complete (no DB)! Once FORESIGHT_DB_URL is set, run 'foresight init'."
    echo ""
    echo "  Interactive TUI:    foresight tui"
    echo "  CLI commands:       foresight --help"
    echo "  MCP server:         FORESIGHT_DB_URL=<dsn> uv run foresight-server"
    echo ""
    exit 0
  fi
fi

# Initialize foresight config and DB
uv run python -m foresight_cli.cli init

echo ""
echo "Setup complete! 🚀"
echo ""
echo "  Interactive TUI:    foresight tui"
echo "  CLI commands:       foresight --help"
echo "  Agent mode:         foresight --agent status"
echo "  JSON mode:          foresight --json status"
echo "  MCP server:         uv run foresight-server"
echo ""
