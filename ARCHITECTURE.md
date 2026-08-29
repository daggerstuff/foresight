# Architecture

Foresight is a persistent memory system for AI agents. One Python core serves
four surfaces: an MCP server, a CLI, a TUI, and a Python SDK. A TypeScript
reimplementation of the CLI (`cli/`) mirrors the wire surface for Node-based
agents.

```
 ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │  MCP server  │   │  Python CLI  │   │  Textual TUI │
 │ (foresight/) │   │(foresight_cli)│  │(foresight_cli)│
 └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
        └──────────────┬───┴──────────────────┘
                       ▼
            ┌─────────────────────┐        ┌──────────────────┐
            │   memory pipeline   │◀──────▶│  companion streams│
            │ capture → extract → │        │  (Redis / Kafka)  │
            │ store → retrieve →  │        └──────────────────┘
            │ decay → distill     │
            └──────────┬──────────┘
                       ▼
          ┌──────────────────────────────┐
          │  Postgres (prod) / SQLite    │
          │  + vector store + graph store│
          └──────────────────────────────┘
```

## Repository Map

| Path | What it is |
| ---- | ---------- |
| `foresight/` | Core Python package — the MCP server and the entire memory engine |
| `foresight_cli/` | Python CLI (Typer) and TUI (Textual), thin shells over the core |
| `cli/` | TypeScript reimplementation of the CLI (Node agents) |
| `packages/foresight-core/` | Shared TypeScript core for `cli/` |
| `apps/docs/` | Docusaurus documentation site |
| `plugins/` | Drop-in integrations (Claude/Copilot hooks, auto-inject scripts) |
| `scripts/` | Operational utilities (migration, diagnostics) |
| `tests/` | Pytest suite (SQLite-backed for speed; Postgres in CI) |

## Core Subsystems (`foresight/`)

- **`server.py`** — the FastMCP server: every tool, prompt, and resource the
  agent sees. Tool arguments are pydantic-validated at the boundary.
- **Memory lifecycle** — `capture.py` (triggered ingestion), `memory_gc.py` /
  `memory_maintenance.py` (reaping), `decay_model.py` (temporal decay),
  `subconscious.py` (context blocks: preferences, pending items, patterns).
- **Retrieval** — `hybrid_retriever.py` fuses keyword, TF-IDF, vector, and
  graph signals; `semantic_search.py` and `clustering.py` support it.
- **Relationships** — `memory_relationships.py` + `graph_store.py` link
  memories to each other (ghost nodes, entity links).
- **Multi-tenancy** — `auth.py` + `tenant_middleware.py`; every store and
  query is scoped by `tenant_id` + `user_id`.
- **Encryption** — `encryption.py`: optional AES-256-GCM at-rest layer with
  key rotation (`FORESIGHT_ENCRYPTION_KEY`).
- **Resilience** — `circuit_breaker.py`, `connection_pool.py`
  (Postgres pool with SQLite fallback), `event_bus.py` (pub/sub),
  `hooks.py` (pre/post memory hooks).
- **Observability** — `telemetry.py` (injection latency/counts),
  `audit.py` (action audit trail), `eval_harness.py` / `proof_benchmark.py`
  (retrieval-quality scoring).

## Data Flow

1. **Capture** — conversation turns hit `process_session_transcript`;
   triggers decide what becomes a memory.
2. **Extract** — entities and relationships are pulled out (LLM with
   rule-based fallback), then encrypted if the key is configured.
3. **Store** — memories land in Postgres (prod) with version history
   (`memory_versions`); embeddings go to the vector store, links to the graph.
4. **Retrieve** — `inject_context` fuses signals through
   `hybrid_retriever`, applies decay weights, and returns the top slice.
5. **Distill** — stable observations graduate into context blocks
   (`auto_distill_context_blocks`) that inject at session start.

## Key Invariants

- **Tenant scoping is not optional.** Every read and write filters on
  `tenant_id` + `user_id`; `tests/test_memory_scope.py` fails the build if
  scope isolation regresses.
- **Secrets live in the environment** (`FORESIGHT_DB_URL`,
  `FORESIGHT_ENCRYPTION_KEY`). Never in source, logs, or tool output — see
  [SECURITY.md](SECURITY.md).
- **SQLite is test-only.** Production uses Postgres via `FORESIGHT_DB_URL`.
- **Lazy imports in the hot path.** The server imports heavy subsystems on
  first use to keep MCP startup fast — this is deliberate (see the ruff
  config's `PLC0415` note in `pyproject.toml`).

## Dependency Direction

```
foresight_cli  ──▶  foresight  ──▶  backends (psycopg / sqlite3 / redis / kafka)
     │                  │
     └── textual/typer  └── fastmcp, pydantic
```

`cli/` (TypeScript) has no runtime dependency on the Python packages; it speaks
the same wire protocols and reads the same Postgres schema.
