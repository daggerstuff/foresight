---
name: Foresight backend requires Postgres
description: The imported "Foresight" project's docs/setup script describe SQLite-as-primary, but the actual server code requires a Postgres DSN.
---

`foresight/server.py::_initialize_backend` raises at startup if `FORESIGHT_DB_URL` is
unset ("SQLite-as-primary is no longer supported"), even though README.md,
INSTALL.md, and `scripts/setup.sh` still describe a zero-config SQLite quick start.

**Why:** discovered when running `foresight system doctor` / `foresight-server --health`
after a fresh `uv sync` — both failed with a Postgres-DSN-required error despite
following the documented steps exactly.

**How to apply:** when setting up or debugging this project, always ensure
`FORESIGHT_DB_URL` is set to a reachable Postgres connection string before running
the CLI or MCP server. In Replit, this is wired via `.env` containing
`FORESIGHT_DB_URL=${DATABASE_URL}` (python-dotenv expands `${VAR}` against the
existing process environment), pointing at the built-in Replit Postgres database.
Don't trust the README's SQLite instructions until the tracked follow-up task
reconciling docs/setup.sh with the Postgres requirement is resolved.
