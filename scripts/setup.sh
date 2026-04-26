#!/usr/bin/env bash

# Exit on any error
set -e

echo "Setting up Foresight MCP..."

# Ensure uv is installed (skip if already present)
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found, installing..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Install Python dependencies (uses pyproject.toml)
uv sync

# Create memory directory with secure permissions
MEMORY_DIR="$HOME/.foresight"
mkdir -p "$MEMORY_DIR"
chmod 700 "$MEMORY_DIR"

# Run initial health check to verify installation
python -m foresight_mcp --health

echo "Setup complete. You can now start the server with: uv run foresight-mcp"
