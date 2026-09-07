# Foresight MCP — Production Deployment Guide

Companion to `INSTALL.md` and `README.md`. This guide documents operational and
deploy-time concerns for Foresight: environment architecture, database backend
topology (Neon PostgreSQL), Redis caching, systemd daemonization, multi-agent fleet
rollout, containerization, and troubleshooting.

---

## 1. Quick Start

```bash
# 1. Fetch source or submodule
git submodule update --init --recursive foresight
cd foresight

# 2. Force virtual environment isolation and install runtime dependencies
unset VIRTUAL_ENV
unset VIRTUAL_ENV_DIR
uv sync --extra all

# 3. Export required environment variables (never commit secrets)
export FORESIGHT_DB_URL="postgresql://user:pass@ep-host.region.neon.tech/foresight?sslmode=require"
export FORESIGHT_IDENTITY="user@account"
export FORESIGHT_BANK_ID="default"

# 4. Smoke test the backend factory
uv run python -c "from foresight.backend import create_backend; b=create_backend(); print(f'Backend: {type(b).__name__}')"
# Expected output: Backend: PostgresBackend

# 5. Run 11-point diagnostics
uv run foresight doctor
```

> **Note**: Postgres is strictly required in production. If `FORESIGHT_DB_URL` is unset
> or invalid, the backend factory raises `RuntimeError`.

---

## 2. Environment Variables Specification

| Variable | Required? | Purpose | Default |
| :--- | :--- | :--- | :--- |
| `FORESIGHT_DB_URL` | **Yes** | PostgreSQL connection DSN (`postgresql://` or `postgres://` with `sslmode=require`). | _(none — must set)_ |
| `FORESIGHT_IDENTITY` | **Yes** | Primary logical agent identity (`user` or `user@account`). Propagated to memories. | `$USER@default` |
| `FORESIGHT_BANK_ID` | Recommended | Tenant/bank namespace for cross-tenant isolation and memory domains. | `default` |
| `FORESIGHT_ENCRYPTION_KEY` | Recommended | 32-byte symmetric master key (hex or base64) for AES-256-GCM envelope encryption. | _(none — optional plaintext)_ |
| `FORESIGHT_REDIS_URL` | _Optional_ | Canonical Redis companion cache URL (`redis://[:pw]@host:port[/db]`). | `""` (in-process cache) |
| `REDIS_URL` | _Optional_ | Fallback Redis connection URL for infrastructure compatibility. | `""` |
| `FORESIGHT_HOST` | _Optional_ | FastMCP streamable HTTP server bind host. | `127.0.0.1` |
| `FORESIGHT_PORT` | _Optional_ | FastMCP streamable HTTP server listen port. | `8764` |
| `FASTMCP_STATELESS_HTTP` | _Optional_ | Set `1` for stateless HTTP to avoid 404 "Session expired" on server restarts. | `1` (in systemd/scripts) |
| `FORESIGHT_ALLOW_UNAUTHENTICATED`| _Optional_ | Set `1` for local agent tooling without per-request bearer tokens. | `0` (enforce auth if set) |
| `FORESIGHT_LLM_PROVIDER` | _Optional_ | LLM provider for synthesis/reflection (`openai`, `anthropic`, `gemini`, `ollama`, `vllm`). | `none` |
| `FORESIGHT_LLM_API_KEY` | _Optional_ | API key for the chosen LLM provider. | _(none)_ |
| `FORESIGHT_LLM_MODEL` | _Optional_ | Model identifier override (e.g. `claude-3-5-sonnet-latest`, `gpt-4o`). | Provider default |
| `FORESIGHT_LLM_BASE_URL` | _Optional_ | Custom base URL for OpenAI-compatible inference endpoints. | Provider default |
| `FORESIGHT_DECAY_INTERVAL_HOURS` | _Optional_ | Background daemon memory decay recalculation interval. | `6` |
| `FORESIGHT_MAINTENANCE_INTERVAL_HOURS` | _Optional_ | Background daemon memory consolidation, archive, and GC sweep interval. | `24` |
| `FORESIGHT_DB_PATH` | _Test Only_ | Local SQLite file path override for isolated test fixtures. | `None` (forces Postgres) |

> **Security Guardrail**: Credentials, connection strings, and encryption keys must
> remain strictly in `.env` or system secret managers. `.env` files must always be
> `chmod 600` and gitignored.

---

## 3. Database Architecture & Neon PostgreSQL Topology

Foresight relies on PostgreSQL 17 with `pgvector` for semantic embeddings, hybrid
retrieval (BM25 + pgvector Reciprocal Rank Fusion), temporal decay curves, and
relational entity tracking.

### Neon Connection Pooling

Neon provides two connection hostnames:
1. **Connection Pooler (`*-pooler.*.neon.tech`)**: Operates via pgBouncer in
   transaction-pooling mode. Ideal for multiple agents and short-lived CLI calls.
2. **Direct Compute (`*.*.neon.tech`)**: Direct TCP connection to the PostgreSQL
   compute node. Required for migrations, long-lived locks, and maintenance sweeps.

```
AI Agents (Claude / OpenCode / Antigravity)
       │
       ▼
Foresight FastMCP Server (:8764)
       │ (psycopg_pool ConnectionPool)
       ▼
Neon Transaction Pooler (:5432)
       │ (pgBouncer)
       ▼
PostgreSQL 17 Compute Node (23 Tables + pgvector HNSW Indexes)
```

### Critical Neon Rules

- **`sslmode=require` is mandatory**: Neon drops unencrypted handshakes.
- **Connection Idle Kill**: Neon automatically terminates connections idle for > 5 min.
  `psycopg_pool` handles this transparently by reconnecting on checkout.
- **Test vs. Production Isolation**: Production uses the `foresight` database; test
  suites auto-route to `foresight_test` to guarantee zero state contamination.
- **23 Public Schema Tables**: All tables (`memories`, `context_blocks`, `entity_nodes`,
  `entity_edges`, `curation_runs`, `reflections`, `temporal_anchors`, etc.) are
  versioned and verified by `foresight doctor`.

---

## 4. Backend Selection Mechanics

Backend selection occurs in `foresight/backend/__init__.py:create_backend()`:

```python
def create_backend() -> DatabaseBackend:
    db_url = os.environ.get("FORESIGHT_DB_URL", "").strip()
    if db_url.startswith(("postgresql://", "postgres://")):
        return PostgresBackend(dsn=db_url)
    raise RuntimeError("FORESIGHT_DB_URL is required (Postgres-only)")
```

- **Prefix Matching**: Schemes must be `postgresql://` or `postgres://`.
- **Driver**: The runtime uses `psycopg` 3.3+ and `psycopg_pool` for high-throughput
  connection pooling with lowercase `dict_row` row factory functions.

---

## 5. Redis Companion Cache & Multi-Process Concurrency

Cross-process shared narrative caching is handled by
`foresight/redis_cache.py:RedisCache` and `RedisCompanion`:

- **Key Schema**: `{prefix}:narrative:{tenant_id}:{user_id}:{sha256_hash}`
- **Auxiliary Shard LRU**: `{prefix}:zset:{tenant_id}:{user_id}` scored by epoch timestamp.
- **TTL**: 7 days (`604,800` seconds) natively enforced via `SETEX`.
- **LRU Eviction**: Caps storage at 10,000 entries per user shard. Oldest entries are
  deleted via pipelined `ZREMRANGEBYRANK`.
- **Credential Masking**: Connection URLs and logs mask auth tokens
  (`rediss://default:***@host:6379`).

If `FORESIGHT_REDIS_URL` or `REDIS_URL` is unset, Foresight falls back to its
in-process thread-safe dictionary cache.

---

## 6. Deployment Topologies

### Topology A: Persistent Systemd User Daemon (Standard Linux / Fleet Host)

This is the standard topology for developer workstations and remote fleet nodes.
The daemon runs on `127.0.0.1:8764` with FastMCP Streamable HTTP.

#### 1. Service Definition (`~/.config/systemd/user/foresight.service`)

```ini
[Unit]
Description=Foresight MCP Streamable HTTP Server
Documentation=https://github.com/daggerstuff/foresight
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/vivi/pixelated/foresight
EnvironmentFile=/home/vivi/pixelated/foresight/.env
Environment="PATH=/home/vivi/.local/bin:/usr/local/bin:/usr/bin:/bin"
Environment=FASTMCP_STATELESS_HTTP=1
Environment=FORESIGHT_HOST=127.0.0.1
Environment=FORESIGHT_PORT=8764
Environment=FORESIGHT_ALLOW_UNAUTHENTICATED=1

ExecStart=/home/vivi/.local/bin/uv run --project /home/vivi/pixelated/foresight --no-active python -m foresight --host 127.0.0.1 --port 8764
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=foresight

[Install]
WantedBy=default.target
```

#### 2. Service Management Commands

```bash
# Enable user lingering so daemon persists after SSH disconnect
loginctl enable-linger "$USER"

# Reload, enable, and start
systemctl --user daemon-reload
systemctl --user enable foresight
systemctl --user restart foresight

# Inspect status and live logs
systemctl --user status foresight
journalctl --user -u foresight -f
```

---

### Topology B: Multi-Agent Fleet Rollout (`scripts/rollout_fleet.sh`)

In distributed environments with multiple agent nodes, `scripts/rollout_fleet.sh`
orchestrates automated updates and health verification:

```bash
# Preview status across all nodes
bash scripts/rollout_fleet.sh

# Apply updates across the entire fleet
bash scripts/rollout_fleet.sh --apply
```

#### Fleet Node Matrix

- **`local`**: `localhost` (Development workstation)
- **`billy`**: `40.160.6.46` (Dedicated inference & task execution host)
- **`gnasty`**: `167.233.25.111` (Secondary agent execution & staging node)

The fleet rollout script ensures:
1. Git checkouts are cleanly fetched and submodules synced.
2. Dependencies are synchronized with `uv sync --extra all`.
3. Database migrations and schema checks are executed.
4. Systemd services (`foresight.service`) are reloaded and verified healthy
   with `foresight doctor`.

---

### Topology C: Containerized Docker Deployment

For container runtimes, use the standardized container pattern:

#### `Dockerfile`

```dockerfile
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml README.md ./
RUN uv sync --extra all --no-dev --frozen

COPY foresight/ ./foresight/
COPY foresight_cli/ ./foresight_cli/

EXPOSE 8764

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://127.0.0.1:8764/mcp || exit 1

ENTRYPOINT ["uv", "run", "--no-dev", "python", "-m", "foresight"]
CMD ["--host", "0.0.0.0", "--port", "8764"]
```

#### `docker-compose.yml`

```yaml
version: "3.8"

services:
  foresight:
    build: .
    ports:
      - "8764:8764"
    environment:
      - FORESIGHT_DB_URL=postgresql://foresight:secret@postgres:5432/foresight?sslmode=disable
      - FORESIGHT_REDIS_URL=redis://redis:6379/0
      - FORESIGHT_IDENTITY=agent-cluster
      - FASTMCP_STATELESS_HTTP=1
      - FORESIGHT_ALLOW_UNAUTHENTICATED=1
    depends_on:
      - postgres
      - redis
    restart: unless-stopped

  postgres:
    image: pgvector/pgvector:pg17
    environment:
      POSTGRES_DB: foresight
      POSTGRES_USER: foresight
      POSTGRES_PASSWORD: secret
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

volumes:
  pgdata:
```

---

## 7. Transport Architecture: Streamable HTTP vs. Stdio

Foresight supports two FastMCP transport models:

```
┌──────────────────────────────────────────────────────────┐
│                   AI Agent Ecosystem                     │
│  (Claude Code / OpenCode / Antigravity / Cursor / Mastra)│
└───────────────┬──────────────────────────┬───────────────┘
                │                          │
        Streamable HTTP                 Stdio
        (port 8764)                (subprocess spawn)
                │                          │
                ▼                          ▼
    ┌─────────────────────────┐   ┌────────────────────────┐
    │  Shared systemd Daemon  │   │ Dedicated Subprocess   │
    │  (FASTMCP_STATELESS=1)  │   │ (Isolated per session) │
    └───────────┬─────────────┘   └────────────┬───────────┘
                │                              │
                └──────────────┬───────────────┘
                               ▼
              PostgreSQL 17 (Neon) + Redis
```

### Why Streamable HTTP is Preferred for Multi-Agent Work

1. **Zero Cold-Start Latency**: The connection pool and pgvector indexes stay warm
   in memory; tool calls execute in < 25ms.
2. **Stateless Reconnect Safety**: With `FASTMCP_STATELESS_HTTP=1`, clients that cache
   session IDs survive server restarts without 404s.
3. **Cross-Agent Resource Sharing**: Multiple agent tools (Claude Code, OpenCode,
   Antigravity) multiplex over one shared endpoint without connection contention.

---

## 8. Operational Verification & Telemetry

Verify deployment health using the built-in verification suite:

```bash
# 1. 11-point health check
foresight doctor

# 2. System and maintenance telemetry
foresight status

# 3. 9-point proof benchmark suite
foresight prove

# 4. Security & envelope encryption status
foresight security status
```

---

## 9. Troubleshooting & Operational Runbook

| Symptom | Probable Cause | Corrective Action |
| :--- | :--- | :--- |
| `RuntimeError: FORESIGHT_DB_URL is required` | Environment variable missing or not sourced | Export `FORESIGHT_DB_URL` in `.env` or run `install.sh`. |
| `SSL connection closed unexpectedly` | Neon idle-timeout or missing SSL parameters | Append `?sslmode=require` to your DSN. Connection pool auto-reconnects. |
| `warning: VIRTUAL_ENV does not match project` | Outer virtualenv shadowed the runtime | Run `unset VIRTUAL_ENV VIRTUAL_ENV_DIR` or use `--project <dir> --no-active`. |
| `HTTP 404: Session expired` on MCP tool call | Stateful session lost on server restart | Set `FASTMCP_STATELESS_HTTP=1` in the systemd service or wrapper. |
| `Systemd service inactive after SSH logout` | Systemd user session lingering disabled | Run `loginctl enable-linger $USER`. |
| `Port 8764 already in use` | Zombie foresight process running | Check with `fuser 8764/tcp` or `ss -tulpn \| grep 8764` and restart service. |
| `AttributeError: dict_row` | Stale or incompatible `psycopg` install | Run `uv sync --extra all` to install `psycopg>=3.3.4` and `psycopg-pool>=3.3.1`. |
