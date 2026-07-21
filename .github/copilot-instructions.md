# Copilot Instructions for `foresight`

## Build, test, and lint

Use `uv` workflows from CI/publish scripts:

```bash
# Install deps (CI parity)
uv sync --frozen

# Dev deps (ruff, pytest extras, build tools)
uv sync --extra dev

# Run full test suite
uv run pytest tests/ -v --cov=foresight --cov-report=lcov

# Run a single test file
uv run pytest tests/test_eval_harness.py -v

# Run a single test case
uv run pytest tests/test_eval_harness.py::test_eval_harness_runs -v

# Lint (same target as publish workflow)
uv run ruff check foresight_cli/ foresight/

# Build package
uv build

# Validate built artifacts
uv run twine check dist/*
```

Security scanning in this repo is part of the normal workflow:

```bash
pre-commit install
```

This enables the `ggshield` pre-commit hook configured in `.pre-commit-config.yaml`.

## High-level architecture

Foresight has three connected surfaces:

1. `foresight/server.py` is the runtime core. It defines the FastMCP server, middleware stack, tool contracts (memory, context blocks, curation, temporal/status), startup lifecycle, and schema initialization/migrations.
2. `foresight_cli/` is a Typer CLI facade over server exports. Command modules call the same core functions used by MCP tools, with output mode handling in `foresight_cli/utils/output.py` (`human`, `agent`, `json`).
3. `foresight/backend/` provides backend abstractions and DB adapters. Runtime is Postgres-first (`backend_factory.create_backend`), while SQLite code remains for compatibility/testing paths.

Important system flows:

- **Identity/tenant isolation**: `TenantMiddleware` resolves identity from request metadata/args and writes request-scoped contextvars (`tenant_context.py`), then all DB/tool operations read the current scope (`get_current_account_id()` etc.).
- **Retrieval and injection**: `inject_context` routes through `HybridRetriever` (keyword + TF-IDF + graph + temporal signals) and optional payload budgeting (`injection_budget.py`).
- **Async curation**: `manage_curation_runs` creates and tracks background curation jobs, backed by persisted run records plus operation queue/worker plumbing.
- **Context continuity**: context blocks are first-class (`manage_context_blocks` + `context_blocks.py`) and persisted in DB, with compatibility aliases for legacy subconscious naming.

## Key conventions in this codebase

- **Postgres DSN is required at runtime**: use `FORESIGHT_DB_URL`. Do not implement new SQLite-primary behavior in server runtime paths.
- **Tenant-aware data access is mandatory**: memory and related queries are scoped by both `user_id` and current tenant/account id. Preserve this in any new queries.
- **Tool contracts use typed action envelopes**: server tools accept Pydantic action models (e.g., `MemoryAction`, `ContextBlockAction`, `CurationRunAction`) rather than loose kwargs.
- **Context/curation tool responses use stable JSON envelopes**: success/failure should remain `{"ok": ..., "action": ...}` with `error.message` on failure (`_tool_response`, `_tool_error`).
- **Cross-backend SQL compatibility is intentional**: many call sites use SQLite-style placeholders (`?`), translated by the Postgres backend shim. Follow existing patterns instead of introducing mixed placeholder styles ad hoc.
- **Terminology migration is active**: prefer `context block` naming in new public surfaces, but keep `subconscious` aliases where backward compatibility is required.
- **Environment bootstrap pattern**: CLI/server entrypoints load `.env` by walking candidate paths (cwd, project root, home). Reuse that behavior when adding new entrypoints.
