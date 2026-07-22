# Foresight

Persistent memory for AI agents — Python CLI, TUI, MCP server, and Python SDK, plus an
experimental TypeScript CLI reimplementation (`cli/`). Imported from GitHub
(daggerstuff/foresight).

## Stack

- Python 3.12, managed with `uv` (see `pyproject.toml` / `uv.lock`)
- Backend: **PostgreSQL only** — despite the README describing a SQLite quick-start,
  the current code (`foresight/server.py::_initialize_backend`) requires `FORESIGHT_DB_URL`
  and raises at startup without it ("SQLite-as-primary is no longer supported").
- MCP server (`foresight-server`) built on FastMCP; runs over stdio by default, or
  `streamable-http` when `FORESIGHT_PORT` is set.
- CLI (`foresight`) built with Typer; TUI via Textual.
- Separate TypeScript CLI package under `cli/` (pnpm/tsup/vitest) — not wired up or
  verified in this setup pass.

## Running in Replit

- Replit's built-in Postgres database is provisioned. `.env` maps
  `FORESIGHT_DB_URL=${DATABASE_URL}` so Foresight picks it up automatically
  (python-dotenv expands `${VAR}` against the existing environment).
- Workflow **"Foresight MCP Server"** runs
  `FORESIGHT_DB_URL=$DATABASE_URL FORESIGHT_HOST=0.0.0.0 FORESIGHT_PORT=8000 uv run foresight-server`,
  serving streamable-HTTP MCP at `http://<repl-domain>:8000/mcp`. The shell expands
  `$DATABASE_URL` at startup — no `.env` file needed. This is a backend/API
  workflow (console output), not a browser UI — nothing renders in the webview.
- The `postgres` uv extra (`psycopg[binary,pool]>=3.2`) must be synced:
  `uv sync --extra postgres`. This was run during setup and installs psycopg 3.x.
- CLI usage from the shell: `uv run foresight --help`, `uv run foresight store "..."`,
  `uv run foresight system doctor`, `uv run foresight tui`.
- One-liner setup script exists at `scripts/setup.sh`, but it predates the
  Postgres-only requirement (still describes SQLite auto-init) — see follow-up task.

## User preferences

- Wants both the CLI and the MCP server working, plus a simple packaged install
  script that any user can run.
