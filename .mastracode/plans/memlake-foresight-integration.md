# Merge MemLake Concepts into Foresight

## Overview

The whitepaper (`/tmp/memlake-whitepaper.pdf`, "Designing for Scale: The Data Infrastructure Behind Hindsight") describes MemLake — the storage engine behind Vectorize's Hindsight agent-memory system. Its core theses: (1) agent memory is an append-only, ever-growing workload, so raw observations must be retained immutably with derived layers rebuilt from them; (2) memory is bitemporal — system time (when recorded) vs. business time (when true in the world) — so point-in-time queries must reconstruct state, not just filter dates; (3) retrieval serves many methods from one copy of data, with the ranking policy traveling per-request; (4) cost is measured in queries, not stored memories; (5) correctness is enforced via invariants and retrieval-quality gates.

Foresight has already absorbed several of these ideas (5-signal RRF hybrid retrieval, pgvector HNSW ANN, write-time supersede detection, TTL, an eval harness). This plan merges the four concepts still missing — **bitemporal validity intervals, an append-only raw layer with provenance and reprocessing, per-request ranking weights, and CI retrieval-quality gates** — into Foresight's existing Postgres/SQLite architecture. No storage-engine rewrite.

## Concept Mapping (whitepaper → Foresight today)

| MemLake concept | Foresight status | Action |
|---|---|---|
| Append-only raw layer; derived views rebuildable | Raw transcripts are discarded after regex extraction (`capture.py`); `events` table is a system event log, not raw content | **Phase 2**: `raw_events` append-only table + provenance + `reprocess` |
| Bitemporal validity; point-in-time state reconstruction | Supersession is flag-only (`is_latest`/`superseded_by`, migration 15); `get_memories_as_of_time` (`temporal_queries.py:223`) naively filters `created_at <= T`, ignores supersession and edit history | **Phase 1**: `valid_from`/`valid_until` intervals; rewrite as-of query |
| One copy, many retrieval arms | Done — 5 signals (keyword, TF-IDF cosine, vector, graph, temporal) fused via RRF over one `memories` table (`hybrid_retriever.py`) | No change |
| Ranking policy travels with the request | Arm *toggles* are per-request (`HybridSearchOptions`), but signal *weights* are global (`rrf_tuning.get_rrf_config`) | **Phase 3**: per-request `signal_weights` |
| Cost = queries, not memories | Done — pgvector HNSW ANN (migration 17), fast-path early termination | No change |
| Retrieval-quality gates + invariants | `eval_harness.py` (PIX-3953) exists with scenarios + baseline diff, but no temporal scenarios and no CI gate | **Phase 4**: temporal eval scenarios + CI regression gate + invariant tests |
| Object storage, leaderless serving, formal verification, fleet scaling | N/A — infra designed for a hosted fleet on S3/GCS; Foresight is self-hosted Postgres-first | Deliberately out of scope |

## Complexity Estimate

- **Size**: Large (4 phases, ~12 files: schema, capture, temporal queries, truth resolution, retriever, server/REST wiring, eval harness, tests, TS wire types)
- **Risk**: Medium — adds columns to the core `memories` table (additive, nullable, backfilled); adds one write to the capture hot path (batch insert, opt-in by default); `get_memories_as_of_time` behavior changes to *more correct* results (superseded facts that were valid at T now appear, post-T supersessions no longer leak)
- **Dependencies**: None new — stdlib + existing stack only. Each phase ships independently.

## Steps

### Phase 1 — Bitemporal validity intervals + correct as-of queries

1. **File**: `foresight/backend/schema_ddl.py`
   **Change**: Add migration **18**: `ALTER TABLE memories ADD COLUMN valid_from TEXT`, `ADD COLUMN valid_until TEXT` (nullable; NULL = still valid). Backfill `valid_from = created_at` in batches. Add index `idx_memories_tenant_user_validity ON memories(tenant_id, user_id, valid_from)` (validate the partial-index approach used in migration 17 for the Postgres dialect; keep both backends working — follow the existing dialect patterns in this file).
   **Why**: Intervals are the data model that makes point-in-time queries answerable; flags alone (`is_latest`) cannot express "was true until T".

2. **File**: `foresight/truth_resolution.py`
   **Change**: When a supersede fires (`is_latest` → 0, `superseded_by` set), also close the old row's interval: `valid_until = <supersede time ISO>`; set the superseder's `valid_from = <supersede time ISO>`. Keep `is_latest` as a derived fast-path flag (current-state queries unchanged).
   **Why**: Supersession is an event in business time; the interval closure is what "outdate" means in Hindsight's lifecycle (and Zep/Graphiti's `invalid_at` edge model).

3. **File**: `foresight/temporal_queries.py`
   **Change**: Rewrite `get_memories_as_of_time` (line 223) to reconstruct state as of T:
   - system-time bound: `created_at <= T` (don't see facts recorded after T)
   - business-time bound: `valid_from <= T AND (valid_until IS NULL OR valid_until > T)`
   - content resolution for edited memories: join `memory_versions` (already append-only, `version`/`created_at`, written from `server.py:1637` and `server.py:2968`) and pick the latest version with `version created_at <= T`; fall back to current content for unversioned rows. Keep the `TemporalQueryResult` shape and the no-tenant-column fallback path.
   **Why**: This is the whitepaper's §4 point-in-time correctness, implemented with the standard bitemporal pattern (Datomic/XTDB lineage; Zep/Graphiti apply it to agent memory).

4. **Tests**: extend `tests/test_temporal.py` — as-of before supersede returns the old fact; as-of after returns the new fact; facts recorded after T never appear; edit at T2 resolves to the pre-edit version content for T1; tenant isolation.

### Phase 2 — Append-only raw layer + provenance + reprocessing

5. **File**: `foresight/backend/schema_ddl.py`
   **Change**: Add migration **19**: `raw_events` (id TEXT PK, tenant_id, user_id, session_id, role, content, captured_at, source, metadata, tombstone INTEGER DEFAULT 0) and `memory_provenance` (memory_id, raw_event_id, tenant_id; PK (memory_id, raw_event_id); index on raw_event_id). Append-only: no UPDATE/DELETE code paths except tombstoning (privacy erasure sets `tombstone=1` and nulls content, preserving row shape for audit).
   **Why**: The "lake" in MemLake — the immutable raw layer all derived state can be rebuilt from.

6. **File**: `foresight/capture.py`
   **Change**: Before `MemoryExtractor.extract` runs, persist each message as a `raw_events` row (single batched INSERT per transcript). Gate behind `FORESIGHT_RAW_RETENTION=1` (opt-in, **default OFF** — this codebase handles clinical/sensitive content: `crisis_detection.py`, `sensitivity.py`, PII scanning in `eval_harness.py`; retention must be a deployment decision). After storing each candidate memory, write its `memory_provenance` links back to the source message ids.
   **Why**: Raw retention + provenance is the whitepaper's core rebuildability thesis; Foresight's extractor is deterministic regex today with an LLM-guided extractor explicitly reserved (`capture.py:247–250`), so re-extraction has concrete near-term value.

7. **Files**: `foresight/server.py` (MCP/CLI wiring — follow how existing maintenance commands are exposed) + capture re-entry
   **Change**: Add a `reprocess` maintenance operation: given user + time window, re-run extraction over `raw_events`, relying on the existing dedupe (`DedupeResult`) so already-stored memories are skipped. Idempotent by construction.
   **Why**: "Derived layers are views" — upgrade the extractor, rebuild the derived layer without losing or duplicating anything.

8. **File**: `foresight/memory_gc.py` (and/or `ghost_cleanup.py` — locate the retention sweep during implementation)
   **Change**: Optional raw retention window config (default: keep forever); erasure requests tombstone instead of delete.
   **Why**: MemLake §5.3 treats deletes/updates as GC over an append-only base — tombstones preserve auditability.

9. **Tests**: new `tests/test_raw_layer.py` — raw rows byte-identical on reprocess (idempotence); provenance roundtrip; tombstone erasure nulls content but keeps shape; tenant isolation on `raw_events`; retention env gate.

### Phase 3 — Ranking policy travels with the request

10. **File**: `foresight/hybrid_retriever.py` (+ `foresight/rrf_tuning.py`)
    **Change**: Add `signal_weights: dict[str, float] | None = None` to `HybridSearchOptions` (line 127). Validate keys ⊆ the five signal names (keyword, tfidf_cosine, vector, graph, temporal); ignore-with-log or reject unknown keys (follow existing options-validation style). When present, these weights replace the `get_rrf_config()` globals for that request only; when absent, behavior is unchanged. Echo effective weights in the `explain` output (per-signal scores are already exposed).
    **Why**: MemLake §4.2 — the ranking policy is part of the query, not the store. A "what changed for this user recently" query wants temporal-heavy weights; a "find the exact API recipe" query wants keyword-heavy. Foresight already has per-request arm toggles; weights complete it.

11. **Files**: `foresight/server.py` (`SearchOptions` → `search_memories` MCP tool), `foresight/rest_api.py` (`SearchBody`, line 96, mirrors options), and the TypeScript wire mirror in `cli/` (locate the SearchOptions-mirroring types during implementation and add the field).
    **Why**: One knob, exposed consistently across MCP, REST, and the TS wire surface — matching how `use_hybrid`/`cascade_depth` are threaded today.

12. **Tests**: new `tests/test_signal_weights.py` — weights change fusion ordering; absent weights → identical results to current; invalid key rejected; explain output echoes weights.

### Phase 4 — Retrieval-quality gates + invariants

13. **File**: `foresight/eval_harness.py`
    **Change**: Add temporal/supersede scenarios to the fixture set: a contradiction pair ("loves therapy" → "hates therapy") queried before and after the supersede point; an edit-history scenario; a provenance scenario (memory traceable to raw event). Use the existing `baseline_diff` support.
    **Why**: MemLake §8.4 gates releases on retrieval quality; temporal correctness is exactly the kind of change that silently regresses retrieval.

14. **Files**: CI config (`.github/workflows/…` — locate the existing test workflow) + a committed eval baseline JSON
    **Change**: Run the eval harness as a test/CI step; fail on regression vs. committed baseline beyond threshold.
    **Why**: Turns the harness from a manual tool into the whitepaper's quality gate.

15. **Tests**: new invariant suite `tests/test_memlake_invariants.py` — (a) raw layer append-only (reprocess produces no raw mutations), (b) no orphan supersessions (`superseded_by` resolves to a live memory; `valid_until > valid_from`), (c) as-of monotonicity (as-of T1 ⊆ as-of T2 for T1 < T2 within a supersede chain), (d) tenant isolation across the new tables and as-of queries.
    **Why**: The whitepaper's central method — invariants enforced by tests because the store itself must be trusted.

## Research Notes (hindsight + alternatives, folded in)

- **Hindsight/MemLake (primary source)**: concepts adopted per the mapping table; infrastructure-scale ideas (object storage, leaderless, formal verification) rejected as out of scope for a self-hosted Postgres-first service.
- **Zep/Graphiti**: `valid_at`/`invalid_at` edge intervals and "episodes" (retained raw narrative logs) independently validate Phases 1 and 2. Foresight's `memory_versions` ≈ their entity edit history.
- **Mem0**: ADD/UPDATE/DELETE decision ops ≈ Foresight's `truth_resolution`; interval closure is the strictly more expressive model.
- **Letta/MemGPT, Cognee**: memory blocks / extract-cognify-load — Foresight already has context blocks (`block_registry.py`) and the capture pipeline; no adoption needed.
- Note: this research is from the whitepaper itself plus training knowledge; no live web access was available in this session. Flagged where a fact should be re-verified against Hindsight's current docs during implementation (their API details may have moved).

## Verification

- **Per phase**: run the full pytest suite (SQLite default) plus the new tests listed in each phase; run the Postgres-backend test path if configured in the repo (check `tests/` for a Postgres marker/env; migration 18/19 must apply cleanly on both dialects — test against a copy of a real DB before merging).
- **Manual smoke**: start the MCP server with `FORESIGHT_SUPERSEDE_DETECTION=1` and `FORESIGHT_RAW_RETENTION=1`; store a contradicting pair; verify `search_memories` down-ranks the superseded fact; verify `get_memories_as_of_time` before/after the supersede returns old/new respectively; run `reprocess` twice and confirm no duplicates.
- **Regression check**: eval harness before/after — no baseline regression on non-temporal scenarios (the retriever fusion path for the no-weights case must be bit-identical).

## Risks & Decision Points

- **Raw retention default OFF** (recommended): clinical/sensitive-content deployments must not silently start persisting transcripts. If you want rebuildability out of the box, flip the default — say so and I'll invert the env var.
- **Migration 18 backfill** on large `memories` tables: batch the UPDATE; SQLite `ALTER TABLE ADD COLUMN` is metadata-only, backfill is the only cost.
- **Capture hot path**: one extra batched INSERT per transcript, only when retention is enabled; no per-message round trips.
- **As-of query behavior change** is intentional and more correct — changelog it explicitly.
