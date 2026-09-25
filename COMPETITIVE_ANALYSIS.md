# Foresight vs mem0 vs Supermemory — Gap Analysis and Enhancement Plan

**Question:** what are mem0 and Supermemory doing differently that Foresight is missing, what does Foresight lose without it, and what should it absorb?

**Method:** Foresight findings are read directly from this repository's source and schema (file:line cited). Competitor findings come from `agent-memory-feature-report.md`, which was built from 34 direct doc fetches. Vendor numbers are marked as claims; they are not independent.

---

## 0. The short version

| | mem0 | Supermemory | Foresight today |
|---|---|---|---|
| Core bet | ADD-only extraction + ranking-time decay + async consolidation | Typed fact graph + "dreaming" batch extraction + profiles | Rich memory *lifecycle* engine, privacy-first, MCP-native |
| Ships as | Library + Docker server + managed Platform | Single binary + managed cloud | MCP server + CLI + TUI + SDK |
| Retrieval quality | Neural embeddings + optional reranker | Neural embeddings (bge-base 768d) + cross-encoder | **Feature-hash embedder, no reranker** |
| Ingestion breadth | Text + images + PDFs | Text + PDF/OCR + Office + images + audio/video + connectors | **Text only** |
| Dev surface | REST/SDK/MCP/webhooks | REST/SDK/MCP/FS abstraction | **MCP + CLI + TUI only** |
| Best features locked to cloud? | Yes, substantially | Yes (connectors, MCP) | **No — fully self-hostable** |

**The headline:** Foresight is not behind on memory *semantics* — its data model is arguably the most sophisticated of the three. It is behind on **retrieval quality, ingestion breadth, and developer surface** — the plumbing that makes those semantics reachable. Meanwhile it holds the one position both competitors gave up: a fully self-hostable, privacy-preserving system whose best features are not paywalled.

---

## 1. What they do differently (the three real architectural bets)

### 1.1 mem0 deleted write-time reconciliation — and that is a signal

mem0's v3 replaced the famous ADD/UPDATE/DELETE/NOOP pipeline with **single-pass ADD-only**. One LLM call extracts facts; there is no diff against existing state, and `add()` returns ADD events only. Vendor rationale: "the model spends its capacity on understanding the input rather than diffing against existing state" ([migration](https://docs.mem0.ai/migration/oss-v2-to-v3)).

Consolidation moved *out* of the write path into two places:
- **Ranking-time:** "Memory Decay" multiplies score by a 0.3×–1.5× factor. It is a soft bias, never a filter.
- **Background:** "Dream" — Synthesis (pattern memories), Supersede (mark contradicted fact superseded), Merge (fold duplicates). Nothing Dream does is destructive.

**What this tells us:** write-time reconciliation was expensive and lossy. Keep everything, rank by recency/entity/temporal fit, resolve truth asynchronously and reversibly. Notably, mem0 still scores **32.5 on contradiction_resolution** at 10M tokens — their own benchmark admits ADD-only is weak exactly where reconciliation used to help. This is an *open* problem, not a solved one.

mem0 also removed graph memory from OSS entirely (~4000 lines of Neo4j/Memgraph/Kuzu/AGE/Neptune drivers). Platform graph is now **untyped co-occurrence that affects ranking only**.

### 1.2 Supermemory's graph is typed, and typed edges change what is "true"

Supermemory's three edge semantics are the sharpest single idea in either product:

| Edge | Meaning |
|---|---|
| `updates` | new fact replaces the old **for search purposes**; `isLatest` keeps history |
| `extends` | adds detail without invalidating the old fact |
| `derives` | an **inferred** fact never stated in one place |

Edges are returned inline (`include.relatedMemories` → `parents`/`children` with a `relation`). Inferred facts are flagged `isInference: true`, **down-weighted in search**, and queued for human review (`GET .../inferred`, `POST .../review` with `approve|decline|undo`).

**This is the thing worth copying.** It is expressible as a plain relational table with an `edge_type` enum — exactly the kind of thing Postgres does well.

### 1.3 Supermemory's "dreaming" decouples extraction from ingestion

Document `status: done` means only *chunks* are indexed. Memories come from a **second phase** ("dreaming") where related documents are grouped so memories form from *coherent units* (a real session), not isolated writes. `dreaming: "dynamic"` is the default. mem0 extracts synchronously inside `add`; Supermemory batches.

Both then build **profiles** — static (stable facts) + dynamic (recent context) + buckets — on the rationale that search retrieves what is *similar to the query*, so facts that must always be known (name, pronouns, tone) will never surface.

---

## 2. Where Foresight is already ahead — do not rebuild these

This matters, because a naive reading of the competitor docs would produce a roadmap that reinvents things Foresight already has, sometimes in better form.

1. **Typed edge taxonomy already exists and is broader.** `memory_relationships` constrains `relationship_type` to `updates`, `extends`, `derives`, `contradicts`, `supports`, `related` (`foresight/backend/schema_ddl.py:152`). Supermemory ships three of these; mem0 ships an untyped co-occurrence graph. Foresight additionally has a second, finer taxonomy on entities: `mentions`, `located_at`, `experienced`, `caused`, `relates_to`, `contradicts`, `supports`, `part_of`, `created` (`entity_relationships`, schema v12). And `link_memories` exposes typed edge creation as a first-class MCP tool (`server.py:6284`).

2. **Decay is more principled than mem0's.** Foresight has a real Ebbinghaus model — `current_strength` stored separately from creator `importance` so decay never overwrites the original signal, per-`(tenant,user,category)` config, activation boost, and every application audited in `memory_decay_events` (`decay_model.py`, schema v9). mem0's is a stateless 0.3×–1.5× multiplier tracking at most 20 timestamps.

3. **Context blocks are a genuinely unique primitive.** `user_preferences`, `pending_items`, `session_patterns` are always-on scratchpads injected alongside memories (`subconscious.py`, `context_blocks` table), with block registry and injection-point assignment. Neither competitor has an equivalent — Supermemory's profile buckets are the nearest, but they are derived, not hand-maintained.

4. **Curation runs are Dream with a human in the loop.** `manage_curation_runs` does dedup, contradiction detection, stale archival, and synthesis as async jobs with `preserve`/`rebalance` policy modes and `observe`-first safety, staging high-impact changes for review (`server.py:3698`). mem0's Dream Synthesis is opt-in Pro+, scheduled, and up to 24h delayed.

5. **Privacy and clinical posture is a moat, not a checkbox.** Default embedder makes **no external API calls** by design; AES-256-GCM envelope encryption with key rotation; mandatory `tenant_id`+`user_id` scoping enforced by a failing test; audit trail; rate limiting; OAuth. Layer on crisis detection, the Socratic Gate, and empathy metrics (`memory_components.py`, `crisis_detection.py`) and Foresight is the only one of the three built for HIPAA/sensitive deployments.

6. **Operational surface is broader.** Kafka/Redis companion streams, CRDT sync with vector clocks, WebSocket subscriptions, event bus with HTTP-webhook hooks, circuit breakers, an eval harness, and a proof benchmark.

7. **MCP is native and self-hostable.** Prompts (`session_catchup`, `curate_review`, `user_profile`, `foresight_autocontext`), streaming resources (`foresight://context-blocks`, `system-status`, `curation-runs`), and a local dashboard. mem0's MCP is hosted-only; Supermemory's is cloud-only.

---

## 3. What Foresight is missing, and what it loses without it

Ranked by (impact × how cheap it is to close).

### 3.1 Retrieval quality is the biggest gap — and it is self-inflicted

**Finding.** `get_embedder()` accepts exactly one provider: `local-hash`.

```python
# foresight/semantic_search.py:119
def get_embedder(provider: str = DEFAULT_PROVIDER) -> Embedder:
    if provider != DEFAULT_PROVIDER:
        raise SemanticSearchError(f"unknown embedder provider {provider!r}; valid: {sorted(VALID_PROVIDERS)}")
    return LocalHashEmbedder()
```

`VALID_PROVIDERS = frozenset({"local-hash"})`. `LocalHashEmbedder` is **384-dim feature hashing** — a bag-of-words projection. It is *lexical, not semantic*. It largely duplicates the keyword and TF-IDF signals already in the fusion.

**Compounding it:**
- **No reranker.** Zero hits for `rerank`/`cross-encoder` across the repo. Supermemory ships `rerank: true` (+~100ms); mem0 ships five OSS rerankers.
- **No query rewriting.** Supermemory's `rewriteQuery` expands short queries with no extra cost.
- **The vector store is not actually pgvector.** `memory_embeddings.vector` is `BLOB NOT NULL` and cosine is computed in Python — there is no `vector` column type and no HNSW/IVFFlat index in `schema_ddl.py`, despite the README/ARCHITECTURE describing "PostgreSQL 17 + pgvector". So there is no ANN index; similarity is a linear scan.

**What is lost without it.** The five-signal RRF fusion (`hybrid_retriever.py`) is genuinely good architecture — but four of its five signals are lexical (keyword, TF-IDF cosine, hash-vector) or structural (graph, temporal). Only the hash vector is *called* "vector". A user asking a paraphrased question — no shared vocabulary with the stored memory — retrieves poorly. This is the mechanism behind the most common memory-system failure: "it stored the right thing but surfaced the wrong thing." Every sophisticated subsystem downstream inherits that ceiling.

**Fix (high impact, moderate cost).** Implement the `Embedder` protocol against a real model and register it. The protocol already exists and `embedding_validation.py` already dimension-checks, so this is a contained change:
- Add providers (`fastembed`/`sentence-transformers` local, OpenAI, Ollama) behind `VALID_PROVIDERS`.
- Store a `provider`+`dimension` per row (already in the schema PK) so mixed/upgraded embeddings coexist.
- Add pgvector: `ALTER TABLE memory_embeddings ADD COLUMN embedding vector(768)` + HNSW index, backfill, and keep the BLOB path for SQLite tests.
- Add a reranker stage after RRF fusion, default off, with mem0-style `explain=True` score breakdowns (mem0's `score_details` is a real debuggability win Foresight lacks).

### 3.2 Ingestion is text-only; competitors ingest the world

**Finding.** The `documents`/`document_chunks` layer is a real asset — it tracks source, `content_hash`, character spans, and supports re-derivation — but its two extraction paths are heuristic paragraph chunking and LLM text extraction (`document_layer.py`). There is no image, audio, video, PDF, or OCR path anywhere (the earlier `ocr` match was "So**cr**atic").

**What is lost without it.** Supermemory ingests PDF (with OCR), Office, images (visual + diagram interpretation), audio/video (transcription + speaker detection), and AST-aware code chunking, plus connectors for Drive, Gmail, Notion, OneDrive, GitHub, S3, and a web crawler. Foresight cannot absorb a PDF, a screenshot, a meeting recording, or a codebase. For a system explicitly built for clinical/sensitive work, "cannot ingest a scanned document" is a functional disqualifier.

**Fix (staged).**
1. Add a `file` ingest path that extracts text then reuses the existing document→chunk→memory pipeline. Start with PDF (`pypdf`) + plain-text/Markdown/code; OCR (`tesseract`/`ocrmypdf`) second.
2. Add **type-aware chunking** — reuse `code-chunk`-style AST splitting for code, heading hierarchy for Markdown, semantic sections for PDF. This is cheap and immediately improves extraction quality.
3. Add a connector interface (`Connector` protocol, `connections` table, sync cursor, webhook receiver) *before* writing any specific connector. Cloud-only in both competitors — a self-hostable connector is an unclaimed position.

### 3.3 There is no HTTP API — the semantics are unreachable from most stacks

**Finding.** The server is FastMCP with starlette custom routes for `/health` and a local `/ui/*` dashboard. There is no REST API and no OpenAI-compatible endpoint. The TypeScript CLI does not call an API — it reads Postgres directly (`ARCHITECTURE.md`).

**What is lost without it.** mem0 and Supermemory both expose REST + Python/JS SDKs, batch endpoints, and webhooks. Foresight requires the caller to be an MCP client or to speak Postgres. That excludes every non-MCP framework (LangChain, CrewAI, Vercel AI SDK, plain HTTP services), makes writes from webhooks and connectors awkward, and forces the TS CLI into a direct-DB coupling that is a security and schema-coupling liability.

**Fix.** Add a thin REST layer over the existing tool handlers — they are already pydantic-validated at the boundary, so this is mostly routing and auth. Priorities: `POST /memories`, `POST /search`, `POST /inject`, `POST /documents`, `GET /profile`, batch endpoints. Add an OpenAI-compatible memory tool endpoint only if it is cheap; REST is the unlock.

### 3.4 Typed edges exist in schema but not in behavior

**Finding.** This is the subtlest and most important gap, because it looks solved from the schema:

- The `relationship_type` enum exists (`schema_ddl.py:152`).
- `capture.py` writes `derives` edges (lines 507, 595).
- `link_memories` lets an agent create `updates`/`extends`/`derives`/`contradicts`/`supports`/`related`.

But:
- **No automatic `updates` detection at write time.** Nothing decides that a new memory supersedes an old one and marks it.
- **No truth-enforcement in retrieval.** There is no `is_latest`/superseded flag, and the retriever's graph signal is *entity-based expansion* (`hybrid_retriever.py` docstring) — I found no evidence it consumes `memory_relationships` to demote superseded facts. `superseded` appears only in a proof-benchmark string literal.
- **No inference governance.** No `is_inference` flag and no review queue for `derives` facts.

**What is lost without it.** The system can *record* that fact B updates fact A and still let fact A win the ranking. That is worse than not having the edge, because it creates the impression of contradiction handling without the behavior. And ungoverned `derives` edges let the system infer something the user never said and then treat it as fact — the exact failure Supermemory's down-weighting + review queue prevents.

**Fix (highest leverage per line of code).**
1. Add `is_latest BOOLEAN DEFAULT true`, `inferred BOOLEAN DEFAULT false`, and `superseded_by` to `memories`; add `review_status` to metadata.
2. On write, run a cheap contradiction/supersede check against the top-K semantically similar memories (reuse `enhanced_synthesizer`'s contradiction detection, already built, `enhanced_synthesizer.py`): write an `updates` edge and set `is_latest=false` on the older memory.
3. In `hybrid_retriever`, apply a **ranking penalty** to `is_latest=false` and `inferred=true` rather than filtering — reusing the existing decay/deca-multiplier plumbing. Never hard-filter; mem0's lesson is that over-filtering loses recall.
4. Add `manage_inferred` actions (`list`, `approve`, `decline`, `undo`) following Supermemory's semantics. This is the single most defensible *novel* feature Foresight could ship, because neither competitor does it well automatically.

### 3.5 Smaller, concrete gaps

| Gap | Evidence | What is lost |
|---|---|---|
| **No memory expiration / TTL** | `expires_at` exists only on `auth_sessions`; no `expires_at`/`forget_after` on `memories` | Temporary facts ("standup at 3pm today") live forever and pollute injection budget. Both competitors support it (`expiration_date`, `forgetAfter`). |
| **No org/project hierarchy** | `tenants` + `users` only | Cannot model org → project → member the way mem0 Platform and Supermemory do. Blocks team/enterprise sales. |
| **Profile built but not exposed** | `synthesize_profile` defined at `server.py:5823` but **not decorated `@mcp.tool`**; only the CLI (`foresight_cli/commands/analysis.py:72`) and the `user_profile` prompt reach it | Agents cannot call the profile. This is a one-line fix with outsized value — Supermemory treats profiles as core. |
| **No profile buckets** | `profile_synthesizer.py` has static/dynamic only | No third topical axis (preferences/goals/work). Cheap to add given the layers already exist. |
| **No async write status** | writes are synchronous (background threads exist but no job handle returned) | Callers cannot fire-and-forget and poll. mem0 returns `PENDING`+`event_id`; Supermemory returns `status: queued→done`. |
| **No batch operations** | single-memory calls only | mem0 does 1000/call; bulk ingest is impractical. |
| **No soft-delete recovery surface** | `archived_at` exists on `memories` | Archive is present but there is no `include_archived` / restore semantics exposed equivalent to Supermemory's `include.forgottenMemories`. |
| **No published benchmark numbers** | `eval_harness.py` + `proof_benchmark.py` are internal | Cannot make a credibility claim. mem0 publishes (Platform-only) numbers; Supermemory publishes none but ships MemoryBench. |
| **No agent self-signup** | — | mem0's `mem0 init --agent` lets an agent provision itself in <30s. Relevant to Foresight's agent-first positioning. |
| **No query rewriting** | 0 hits | Short queries retrieve poorly. Supermemory merges multiple rewrites at no extra cost. |

---

## 4. Recommended roadmap

**Tier 1 — retrieval credibility (do first; everything else is capped by this)**
1. Real `Embedder` providers behind `VALID_PROVIDERS` (local fastembed default to preserve the no-egress guarantee; OpenAI/Ollama opt-in).
2. pgvector column + HNSW index; keep BLOB path for SQLite tests.
3. Reranker stage after RRF, default off, with `score_details` explain output.
4. `is_latest` / `inferred` + supersede detection on write + ranking penalties in the retriever.
5. `manage_inferred` review queue (`list`/`approve`/`decline`/`undo`).

**Tier 2 — reach and breadth**
6. REST API over existing handlers (`/memories`, `/search`, `/inject`, `/documents`, `/profile`, batch).
7. Memory expiration/TTL (`expires_at`, hidden from search unless requested).
8. Expose `synthesize_profile` as an MCP tool; add profile buckets.
9. Document ingestion: PDF + Markdown/code + type-aware chunking, then OCR.

**Tier 3 — differentiation and scale**
10. Connector interface + first connector (local filesystem/S3 are the self-hostable differentiators).
11. Async write + status polling; batch endpoints.
12. Publish LoCoMo/LongMemEval numbers using the existing eval harness — report **accuracy, tokens/query, and latency at a fixed retrieval budget**, with a declared judge. This is the credibility move neither competitor can fully copy, because both gate their best features to the cloud.

---

## 5. The strategic read

mem0 and Supermemory are converging on the same conclusion: **stop reconciling at write time, keep everything, and resolve truth asynchronously and reversibly.** mem0 does it with an ADD-only extractor plus ranking-time decay plus Dream. Supermemory does it with typed edges plus inference flagging plus a review queue.

Foresight already owns the hardest parts of that convergence — a real decay model, typed edges, curation runs, context blocks, an audit trail, encryption, and tenant isolation enforced by tests. What it lacks is not memory *thinking*; it is the **embedding, ingestion, and HTTP plumbing** that makes the thinking visible to a caller.

The one thing both competitors gave up and Foresight still holds: **fully self-hostable memory with no feature paywall.** mem0's graph, decay, temporal reasoning, Dream, webhooks, and profiles are Platform-only, and its published numbers come from managed infrastructure. Supermemory's connectors and MCP are cloud-only and it publishes no numbers at all.

So the highest-value move is not to imitate their cloud features. It is to close the retrieval-quality gap, add an HTTP surface, add real ingestion, and then **publish reproducible benchmarks for the self-hosted build** — the position neither competitor can occupy without cannibalizing its cloud revenue.

---

*Companion source: `agent-memory-feature-report.md` (competitor feature inventory, citations, benchmark methodology).*
