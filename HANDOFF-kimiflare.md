# Handoff: Foresight Test-Suite Migration Debt

**For:** kimiflare
**From:** prior session (continued from Opencode's `session-ses_08e6.md` investigation)
**Date:** 2026-07-17
**Repo:** `/home/vivi/pixelated/foresight` (submodule; Python/FastMCP + Postgres backend)

---

## TL;DR

The Foresight **runtime service is healthy and fully functional** — it starts via
systemd, binds port `8764`, and exposes all 8 MCP tools correctly over a real
`initialize` → `tools/list` handshake. The outstanding work is **test-suite
migration debt**: ~65 tests still assume the old **SQLite dual-store** design and
break against the now **Postgres-only** backend. They need to be ported to Postgres
(or given a test Postgres DB + backend init fixture). This is a test-only problem;
no production code is broken.

---

## What is already fixed (do NOT re-do)

These were completed before this handoff and verified:

1. **Log-noise bug** — `foresight/foresight/tenant_middleware.py`:
   `_sanitize_id()` now returns `None` silently for `None`/non-string values instead
   of logging `Rejected invalid ID from request context: None` on every normal
   request. Genuinely invalid *string* IDs still warn.
   - Regression tests added in `foresight/tests/test_tenant_middleware.py`
     (`test_none_identity_value_is_silent`, `test_invalid_string_identity_is_rejected`).
   - This test file is green (7 passed).

2. **Client config** — `~/.claude.json` (the MCP server block) previously pointed at a
   **nonexistent launcher** `scripts/memory/foresight-mcp-server.sh` (the real file is
   `foresight-server.sh`) and used the deprecated `FORESIGHT_USER_ID`. Corrected to the
   real launcher path + `FORESIGHT_IDENTITY: "vivi"`.
   - **This was the actual cause of "no tools are exposed" from the Claude Code client
     perspective** — the server never connected because the launcher path was wrong.
     The server itself was always fine.

3. **systemd unit** — `~/.config/systemd/user/foresight-mcp.service`: swapped
   `Environment=FORESIGHT_USER_ID=vivi` → `FORESIGHT_IDENTITY=vivi`. `ExecStart`
   already correctly referenced `foresight-server.sh`.

4. **Docs** — `scripts/memory/README.md`: removed stale "standalone clone" / SQLite /
   `FORESIGHT_USER_ID` language; now reflects repo-submodule wrapper, `FORESIGHT_DB_URL`
   (Postgres) override, and `FORESIGHT_IDENTITY` as the single user env var.

> Note: items 2 and 3 are **user-level external config** (outside the repo), edited
> directly on the environment. They are not part of a git commit.

---

## The actual problem: SQLite → Postgres test debt

### Root cause
`foresight/foresight/server.py` was migrated to be **Postgres-only** (no SQLite
fallback by design — see `get_db_connection()` at `server.py:536`, which raises
`RuntimeError: Database backend not initialized. Call _initialize_backend() first
(requires FORESIGHT_DB_URL)`). The test suite was **not** migrated alongside it.

### Evidence (from a full run with `.env` loaded, i.e. `FORESIGHT_DB_URL` set)
65 failures, distributed as:

| File | Failures | Nature |
|------|----------|--------|
| `tests/test_server.py` | 48 | Imports `sqlite3`, `SqliteBackend`; patches `FORESIGHT_DB_PATH` to temp file; calls `init_db()` — all SQLite-specific |
| `tests/test_sync.py` | 22 | `temp_db` fixtures create `*.db` temp files; expect SQLite |
| `tests/test_narrative_cache.py` | 22 | SQLite file / `wal` pragma assumptions |
| `tests/test_recovery.py` | 18 | SQLite-specific SQL / file assertions |
| `tests/test_tenant_context.py` | 13 | Backend init expectations |
| `tests/test_tenant_isolation.py` | 8 | Tenant scoping against SQLite |
| `tests/test_clustering_tools.py` | 8 | `Error: near ...` SQLite syntax (`INSERT OR IGNORE`, etc.) |
| `tests/test_capture.py` | 6 | DB-backed |
| `tests/test_reflection_narrative.py` | 6 | DB-backed |
| `tests/test_graph_store_tenant.py` | 2 | DB-backed |

**Error signature breakdown:** 54 × `RuntimeError: Database backend not initialized…`,
8 × `Error: near …` (SQLite SQL syntax), remainder are SQLite file-path assertions.

### Concrete example of the breakage
`tests/test_server.py` (top of file):
```python
import sqlite3
from foresight.backend import SqliteBackend   # <-- class no longer exists in Postgres-only backend
...
@pytest.fixture(autouse=True)
def setup_test_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_memory.db"
    monkeypatch.setenv("FORESIGHT_DB_PATH", str(db_file))   # <-- SQLite path; ignored by Postgres backend
    ...
    from foresight.server import init_db   # <-- now Postgres-based
```
`SqliteBackend` does not exist in the current backend module → `ImportError` /
`RuntimeError` at collection or call time.

`tests/test_sync.py` uses `tempfile.NamedTemporaryFile(suffix=".db")` (`temp_db`
fixture) — a SQLite file model that has no Postgres equivalent without a real DB.

---

## Recommended fix approach (scope: failing files only)

1. **Add a `tests/conftest.py`** that:
   - Reads `FORESIGHT_TEST_DB_URL` (fall back to `FORESIGHT_DB_URL` from `.env`).
   - Calls `foresight.server._initialize_backend()` (or the public init entrypoint)
     in an `autouse` session fixture, creating a throwaway schema / isolated DB
     (e.g. a dedicated test database or schema, wiped per session).
   - Resets the connection pool between tests to avoid cross-test bleed.

2. **Port the SQLite-specific fixtures:**
   - Replace `temp_db` / `tmp_path` SQLite-file fixtures with a Postgres-backed
     equivalent (create tables in the test DB, truncate in teardown).
   - Replace `monkeypatch.setenv("FORESIGHT_DB_PATH", ...)` with backend init against
     the test DB URL.

3. **Replace SQLite SQL with Postgres-compatible SQL:**
   - `INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`.
   - Remove `wal`/SQLite pragma usage.
   - `sqlite3` imports → remove; use the backend connection interface.

4. **Verify** with:
   ```bash
   cd /home/vivi/pixelated/foresight
   set -a && . /home/vivi/pixelated/.env && set +a
   uv run --no-active -m pytest tests/ -q
   ```
   (The `.env` load matters — `FORESIGHT_DB_URL` must be in the process env, which a
   bare `pytest` invocation does NOT source. The service wrapper
   `scripts/memory/foresight-server.sh` does source it.)

---

## Env / infra facts (so you don't rediscover them)

- **Postgres is available.** Local `postgres:17` Docker container is up
  (`pixelated-postgres`, port 5432). Prod `FORESIGHT_DB_URL` points at Ghost Postgres
  `tsdb` (set in `/home/vivi/pixelated/.env`, line ~480).
- **Service runs via systemd user unit:** `foresight-mcp.service`, launched by
  `scripts/memory/foresight-server.sh` (stdio→SSE when `FORESIGHT_PORT` set; currently
  `--port 8764`).
- **Live tool list (verified):** `manage_memories`, `search_memories`,
  `manage_context_blocks`, `process_session_transcript`, `manage_curation_runs`,
  `inject_context`, `query_memories_temporal`, `get_system_status`.
- **Shell quirk:** the agent shell is `dash`, not bash — use `. file` not `source`,
  and avoid bash substring expansion like `${VAR:0:10}`.

---

## Out of scope / not broken

- No production/runtime bug. The server works; tools are exposed.
- Identity/auth: `FORESIGHT_ALLOW_UNAUTHENTICATED=1` is intentional for local
  stdio; per `AGENTS.md`, auth/security changes need explicit user approval.
- The 7 passing `test_tenant_middleware.py` tests are correct as-is.

---

## Suggested first step for kimiflare

Start with `tests/test_server.py` (48 failures, the largest bucket and the one with
the clearest SQLite assumptions at the top of the file). Get one file green with a
Postgres conftest before touching the others, to validate the fixture pattern before
replicating it.
