# Contributing

Thanks for helping make Foresight better. This repo follows a small set of
non-negotiables — read them once and you're set.

## Setup

```bash
git clone git@github.com:daggerstuff/foresight.git && cd foresight
uv sync --group dev          # installs runtime + dev deps into .venv
export FORESIGHT_DB_URL='postgresql://user:pass@host:5432/db?sslmode=require'
```

No Postgres handy? The test suite runs on SQLite automatically; only a handful
of Postgres-specific paths need the real thing (CI provides one).

## Daily Commands

```bash
uv run pytest tests/ -x -q            # tests (SQLite-backed)
uv run pytest tests/test_foo.py -q    # one file while iterating
uv run ruff check .                   # lint — must pass, zero suppressions
uv run pyright                        # type check
uv run bandit -c pyproject.toml -r foresight foresight_cli scripts
```

## Ground Rules

1. **No error suppression.** No `# noqa` to silence real findings, no
   `# type: ignore` to dodge the checker, no loosening the ruff/pyright
   config to make a failing gate pass. Fix the cause.
2. **Tenant scope everywhere.** Any new store/read path filters on
   `tenant_id` + `user_id`. If you add a storage API, add a scope-isolation
   test next to `tests/test_memory_scope.py`.
3. **Secrets are env-only.** Nothing hardcodes, logs, or echoes
   `FORESIGHT_DB_URL`, `FORESIGHT_ENCRYPTION_KEY`, or provider keys.
4. **SQLite is for tests; Postgres is for prod.** Don't add SQLite-only SQL
   to production paths.
5. **Match the house style.** Lazy imports in the server hot path, singleton
   accessors via module-level `get_*()` functions, pydantic validation at
   tool boundaries. See [ARCHITECTURE.md](ARCHITECTURE.md) for the map.

## CI

Every push and PR runs three gates ([.github/workflows/ci.yml](.github/workflows/ci.yml)):

| Gate | What it does |
| ---- | ------------ |
| `lint` | `ruff check .` |
| `test` | full pytest run with coverage (fails under 60%) + coverage artifact |
| `security` | bandit + Trivy (vulnerabilities + secret scanning) |

Dependabot keeps Python and Actions dependencies fresh — grouped PRs, weekly.

## Pull Requests

- One logical change per PR; rebase on `master`, keep history linear.
- Describe **why**, not just what. Link the issue or ticket if one exists.
- New tools in `server.py` need: schema validation, tenant scoping, tests,
  and a docstring — agents read those docstrings as their only manual.
- If you touch retrieval behavior, run the eval harness
  (`uv run foresight eval-baseline`) and include the before/after numbers.
