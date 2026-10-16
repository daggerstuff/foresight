<div align="center">

<img src="https://raw.githubusercontent.com/daggerstuff/foresight/main/docs/assets/foresight-logo-dark.svg" alt="Foresight" width="220" />

# Foresight

**Memory that persists across the void between sessions.**

[![PyPI](https://img.shields.io/pypi/v/foresight?style=flat-square&color=1a1a2e&labelColor=0f0f1a&logo=pypi&logoColor=white)](https://pypi.org/project/foresight/)
[![Python](https://img.shields.io/badge/Python-3.12+-1a1a2e?style=flat-square&labelColor=0f0f1a&logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-1a1a2e?style=flat-square&labelColor=0f0f1a)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/daggerstuff/foresight/ci.yml?style=flat-square&labelColor=0f0f1a)](https://github.com/daggerstuff/foresight/actions)
[![Security](https://img.shields.io/badge/Security-STRIDE%20Verified-brightgreen?style=flat-square&labelColor=0f0f1a)](THREAT_MODEL.md)

</div>

---

Every conversation ends. The context window closes. What was learned, decided,
felt — gone.

Foresight gives agents a place to put it. Not a log file. Not a prompt trick. A
memory subsystem with semantic search, temporal decay curves, emotional context
tracking, and relationship graphs. Context surfaces when it matters and decays
gracefully when it doesn't.

It runs as a FastMCP server, a CLI, a Textual TUI, or an embeddable Python SDK.
PostgreSQL 17 + `pgvector` holds the truth, while Redis accelerates ephemeral
locks and caches.

---

## 🏗️ Architecture

```mermaid
flowchart TB
    subgraph Clients["Agent Interfaces"]
        IDE["Cursor / MastraCode / Goose"]
        CLI["Foresight CLI & TUI"]
        SDK["Python SDK / FastMCP Client"]
    end

    subgraph Core["Foresight Core Engine"]
        Auth["Tenant Isolation & Middleware"]
        Router["Tool Dispatcher & Rate Limiter"]
        Crypto["AES-256-GCM Envelope Encryption"]
        Retriever["Hybrid Retriever (pgvector + BM25 RRF)"]
        Decay["Temporal Decay Engine"]
        Context["Context Block Engine"]
    end

    subgraph Storage["Persistent Storage"]
        PG[("PostgreSQL 17 + pgvector\n(Neon DB)")]
        Redis[("Redis Cluster\n(Cache & Locks)")]
    end

    Clients --> Router
    Router --> Auth
    Auth --> Crypto
    Crypto --> Retriever
    Retriever --> PG
    Context --> PG
    Decay --> PG
    Router --> Redis
```

---

## ⚡ Quickstart

### Installation

```bash
# Install with uv (recommended) or pip
uv pip install "foresight[all]"

# Configure database DSN
export FORESIGHT_DB_URL="postgresql://user:pass@ep-host.region.neon.tech/neondb?sslmode=require"
export FORESIGHT_ENCRYPTION_KEY="<32-byte-hex-or-base64-key>"

# Run health check & launch TUI
foresight doctor
foresight tui
```

---

## 🛠️ MCP Tool Reference

Foresight exposes a comprehensive FastMCP tool suite for AI agents:

| MCP Tool                     | Primary Description                                                  | Key Arguments                                     |
| :--------------------------- | :------------------------------------------------------------------- | :------------------------------------------------ |
| `inject_context`             | Single-turn context hydration returning active memories & directives | `conversation_text`, `max_memories=5`             |
| `manage_memories`            | Store, update, delete, search, and archive long-term memory units    | `action="store"\|"search"`, `category`, `content` |
| `search_memories`            | Hybrid semantic vector search + keyword rank fusion                  | `query`, `use_hybrid`, `signal_weights`, `limit`  |
| `manage_context_blocks`      | Standing guidance, user preferences, and pending tasks               | `action="get"\|"update"`, `label`, `content`      |
| `query_memories_temporal`    | Time-window, trend, decay, and point-in-time (bitemporal) queries    | `window="week"\|"month"`, `as_of`, `category`     |
| `capture_in_flight_memory`   | Immediate mid-session storage of decisions, fixes, and preferences   | `content`, `category`, `importance`             |
| `process_session_transcript` | End-of-session auto-distillation and decision extraction             | `session_id`, `messages`                          |
| `reprocess_raw_events`       | Rebuild derived memories from the append-only raw layer (idempotent) | `window_start`, `window_end`                      |
| `get_system_status`          | Health telemetry, cache statistics, and memory counts                | `include_trends=True`                             |

---

## ⚙️ Environment Configuration

| Variable                   | Default                    | Description                                         |
| :------------------------- | :------------------------- | :-------------------------------------------------- |
| `FORESIGHT_DB_URL`         | _Required_                 | PostgreSQL connection string with SSL enabled       |
| `FORESIGHT_ENCRYPTION_KEY` | _Optional_                 | Master AES-256-GCM symmetric key (32 bytes hex/b64) |
| `REDIS_URL`                | `redis://localhost:6379/0` | Ephemeral cache and distributed lock store          |
| `FORESIGHT_DEFAULT_TENANT` | `default`                  | Default tenant isolation domain                     |
| `FORESIGHT_LLM_PROVIDER`   | `anthropic`                | LLM extraction provider (`anthropic`, `openai`)     |
| `FORESIGHT_RATE_LIMIT`     | `60`                       | Max tool calls per minute per tenant                |
| `FORESIGHT_RAW_RETENTION`  | `0`                        | Persist raw events for `reprocess_raw_events`      |

---

## 🛡️ Security & Privacy Guardrails

- **Zero PHI / Sensitive Data Leaks**: Built-in PII/PHI redaction filters
  prevent accidental leakage.
- **Envelope Encryption**: Field-level AES-256-GCM encryption with versioned
  cryptographic key identifiers.
- **Tenant Isolation**: Mandatory tenant segregation on every database query and
  embedding lookup.
- For complete threat modeling and trust boundary details, see
  [THREAT_MODEL.md](THREAT_MODEL.md) and [SECURITY.md](SECURITY.md).

---

## 🧪 Development & Testing

```bash
# Run complete test suite
uv run pytest

# Run type check and linting
uv run ruff check .
uv run ruff format --check .

# Run security SAST audit
uv run bandit -c pyproject.toml -r foresight foresight_cli scripts

# Run real-database proof benchmarks
foresight prove
```

## 🚀 Deployment & Operational Runbooks

Foresight supports containerized Docker execution as well as systemd daemon orchestration:
- **Docker Compose**: Launch PostgreSQL with pgvector, Redis, and Foresight MCP server via `docker compose up -d`.
- **Systemd Service**: A reference service definition is provided in `foresight.service` for standalone production host deployment.
- **Connection Pool Tuning**: Configure `MAX_CONNECTIONS` and `POOL_RECYCLE_SECONDS` to prevent connection exhaustion.
- **Circuit Breaker Policies**: Upstream LLM extraction requests fail over automatically when transient provider rate limits occur.

## 📐 Architecture & Subsystem Layout

```text
foresight/
├── server.py              # FastMCP protocol implementation and tool dispatch
├── hybrid_retriever.py    # Reciprocal Rank Fusion (RRF) combining BM25 and vector embeddings
├── graph_store.py         # Knowledge graph entity and relationship store
├── reflection_engine.py   # Background memory consolidation and decay modeling
└── observability.py       # OpenTelemetry, Prometheus, and structured logging adapters
```

## 📊 Telemetry Protocol & Token Economics

Foresight tracks context economics dynamically:
- **Baseline Prompt Tokens**: Estimated context tokens without Foresight synthesis.
- **Injected Tokens**: Compacted token payload returned by `inject_context`.
- **Saved Token Delta**: Cumulative tokens saved per session, reducing agent inference costs.
- **Latency Percentiles**: Rolling p50/p90/p99 query latencies captured in telemetry state.

## 🔌 Client SDKs & Integration Patterns

Foresight provides native integration patterns for agentic execution:
- **Python Async Client**: Direct async RPC access via `foresight.client.ForesightClient`.
- **FastMCP Protocol**: Standardized JSON-RPC tool interface for Claude Code, Cursor, and OpenCode.
- **REST Gateway**: HTTP endpoints mirroring the MCP tools one-to-one
  (`/memories`, `/memories/temporal`, `/search`, `/inject`, `/documents`,
  `/profile`, `/admin/raw-events/reprocess`); the full surface is served at
  `/openapi.json`.
- **Streaming WebSockets**: Live memory synchronization and push notifications.

## ⚡ Database Indexing & Performance

Foresight leverages specialized PostgreSQL indexing structures:
- **HNSW Vector Indices**: Approximate nearest neighbor vector search with cosine distance.
- **pg_trgm Generalized Inverted Index (GIN)**: Sub-millisecond substring and fuzzy phrase lookup.
- **B-tree Partitioning**: Tenant and temporal range indexing ensuring predictable sub-10ms query times.

## 🩺 Health & Readiness Monitoring

- **CLI Status Check**: Run `foresight status` to view active pool connections and cache stats.
- **Database Connectivity**: Verify PostgreSQL SSL connection parameters.
- **Redis Health**: Ping Redis cache instance with `redis-cli ping`.

New here? Start with [CONTRIBUTING.md](CONTRIBUTING.md) for setup and ground
rules, and [ARCHITECTURE.md](ARCHITECTURE.md) for the map of the codebase.

---

<div align="center">

_The best agents remember. The rest repeat their mistakes._

</div>
