# Foresight vs mem0 vs Supermemory — Gap Analysis and Enhancement Plan

**Question:** what are mem0 and Supermemory doing differently that Foresight is
missing, what does Foresight lose without it, and what should it absorb?

**Method:** Foresight findings are read directly from this repository's source
and schema (file:line cited). Competitor findings come from
`agent-memory-feature-report.md`, which was built from 34 direct doc fetches.
Vendor numbers are marked as claims; they are not independent.

---

## 0. The short version

|                                | mem0                                                           | Supermemory                                                 | Foresight today                                           |
| ------------------------------ | -------------------------------------------------------------- | ----------------------------------------------------------- | --------------------------------------------------------- |
| Core bet                       | ADD-only extraction + ranking-time decay + async consolidation | Typed fact graph + "dreaming" batch extraction + profiles   | Rich memory _lifecycle_ engine, privacy-first, MCP-native |
| Ships as                       | Library + Docker server + managed Platform                     | Single binary + managed cloud                               | MCP server + CLI + TUI + SDK                              |
| Retrieval quality              | Neural embeddings + optional reranker                          | Neural embeddings (bge-base 768d) + cross-encoder           | **Feature-hash embedder, no reranker**                    |
| Ingestion breadth              | Text + images + PDFs                                           | Text + PDF/OCR + Office + images + audio/video + connectors | **Text only**                                             |
| Dev surface                    | REST/SDK/MCP/webhooks                                          | REST/SDK/MCP/FS abstraction                                 | **MCP + CLI + TUI only**                                  |
| Best features locked to cloud? | Yes, substantially                                             | Yes (connectors, MCP)                                       | **No — fully self-hostable**                              |

**The headline:** Foresight is not behind on memory _semantics_ — its data model
is arguably the most sophisticated of the three. It is behind on **retrieval
quality, ingestion breadth, and developer surface** — the plumbing that makes
those semantics reachable. Meanwhile it holds the one position both competitors
gave up: a fully self-hostable, privacy-preserving system whose best features
are not paywalled.

---

## 1. What they do differently (the three real architectural bets)

### 1.1 mem0 deleted write-time reconciliation — and that is a signal

mem0's v3 replaced the famous ADD/UPDATE/DELETE/NOOP pipeline with **single-pass
ADD-only**. One LLM call extracts facts; there is no diff against existing
state, and `add()` returns ADD events only. Vendor rationale: "the model spends
its capacity on understanding the input rather than diffing against existing
state" ([migration](https://docs.mem0.ai/migration/oss-v2-to-v3)).

Consolidation moved _out_ of the write path into two places:

- **Ranking-time:** "Memory Decay" multiplies score by a 0.3×–1.5× factor. It is
  a soft bias, never a filter.
- **Background:** "Dream" — Synthesis (pattern memories), Supersede (mark
  contradicted fact superseded), Merge (fold duplicates). Nothing Dream does is
  destructive.

**What this tells us:** write-time reconciliation was expensive and lossy. Keep
everything, rank by recency/entity/temporal fit, resolve truth asynchronously
and reversibly. Notably, mem0 still scores **32.5 on contradiction_resolution**
at 10M tokens — their own benchmark admits ADD-only is weak exactly where
reconciliation used to help. This is an _open_ problem, not a solved one.

mem0 also removed graph memory from OSS entirely (~4000 lines of
Neo4j/Memgraph/Kuzu/AGE/Neptune drivers). Platform graph is now **untyped
co-occurrence that affects ranking only**.

### 1.2 Supermemory's graph is typed, and typed edges change what is "true"

Supermemory's three edge semantics are the sharpest single idea in either
product:

| Edge      | Meaning                                                                     |
| --------- | --------------------------------------------------------------------------- |
| `updates` | new fact replaces the old **for search purposes**; `isLatest` keeps history |
| `extends` | adds detail without invalidating the old fact                               |
| `derives` | an **inferred** fact never stated in one place                              |

Edges are returned inline (`include.relatedMemories` → `parents`/`children` with
a `relation`). Inferred facts are flagged `isInference: true`, **down-weighted
in search**, and queued for human review (`GET .../inferred`, `POST .../review`
with `approve|decline|undo`).

**This is the thing worth copying.** It is expressible as a plain relational
table with an `edge_type` enum — exactly the kind of thing Postgres does well.

### 1.3 Supermemory's "dreaming" decouples extraction from ingestion

Document `status: done` means only _chunks_ are indexed. Memories come from a
**second phase** ("dreaming") where related documents are grouped so memories
form from _coherent units_ (a real session), not isolated writes.
`dreaming: "dynamic"` is the default. mem0 extracts synchronously inside `add`;
Supermemory batches.

Both then build **profiles** — static (stable facts) + dynamic (recent
context) + buckets — on the rationale that search retrieves what is _similar to
the query_, so facts that must always be known (name, pronouns, tone) will never
surface.

---

## 2. Where Foresight is already ahead — do not rebuild these

This matters, because a naive reading of the competitor docs would produce a
roadmap that reinvents things Foresight already has, sometimes in better form.

1. **Typed edge taxonomy already exists and is broader.** `memory_relationships`
   constrains `relationship_type` to `updates`, `extends`, `derives`,
   `contradicts`, `supports`, `related` (`foresight/backend/schema_ddl.py:152`).
   Supermemory ships three of these; mem0 ships an untyped co-occurrence graph.
   Foresight additionally has a second, finer taxonomy on entities: `mentions`,
   `located_at`, `experienced`, `caused`, `relates_to`, `contradicts`,
   `supports`, `part_of`, `created` (`entity_relationships`, schema v12). And
   `link_memories` exposes typed edge creation as a first-class MCP tool
   (`server.py:6284`).

2. **Decay is more principled than mem0's.** Foresight has a real Ebbinghaus
   model — `current_strength` stored separately from creator `importance` so
   decay never overwrites the original signal, per-`(tenant,user,category)`
   config, activation boost, and every application audited in
   `memory_decay_events` (`decay_model.py`, schema v9). mem0's is a stateless
   0.3×–1.5× multiplier tracking at most 20 timestamps.

3. **Context blocks are a genuinely unique primitive.** `user_preferences`,
   `pending_items`, `session_patterns` are always-on scratchpads injected
   alongside memories (`subconscious.py`, `context_blocks` table), with block
   registry and injection-point assignment. Neither competitor has an equivalent
   — Supermemory's profile buckets are the nearest, but they are derived, not
   hand-maintained.

4. **Curation runs are Dream with a human in the loop.** `manage_curation_runs`
   does dedup, contradiction detection, stale archival, and synthesis as async
   jobs with `preserve`/`rebalance` policy modes and `observe`-first safety,
   staging high-impact changes for review (`server.py:3698`). mem0's Dream
   Synthesis is opt-in Pro+, scheduled, and up to 24h delayed.

5. **Privacy and clinical posture is a moat, not a checkbox.** Default embedder
   makes **no external API calls** by design; AES-256-GCM envelope encryption
   with key rotation; mandatory `tenant_id`+`user_id` scoping enforced by a
   failing test; audit trail; rate limiting; OAuth. Layer on crisis detection,
   the Socratic Gate, and empathy metrics (`memory_components.py`,
   `crisis_detection.py`) and Foresight is the only one of the three built for
   HIPAA/sensitive deployments.

6. **Operational surface is broader.** Kafka/Redis companion streams, CRDT sync
   with vector clocks, WebSocket subscriptions, event bus with HTTP-webhook
   hooks, circuit breakers, an eval harness, and a proof benchmark.

7. **MCP is native and self-hostable.** Prompts (`session_catchup`,
   `curate_review`, `user_profile`, `foresight_autocontext`), streaming
   resources (`foresight://context-blocks`, `system-status`, `curation-runs`),
   and a local dashboard. mem0's MCP is hosted-only; Supermemory's is
   cloud-only.

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

`VALID_PROVIDERS = frozenset({"local-hash"})`. `LocalHashEmbedder` is **384-dim
feature hashing** — a bag-of-words projection. It is _lexical, not semantic_. It
largely duplicates the keyword and TF-IDF signals already in the fusion.

**Compounding it:**

- **No reranker.** Zero hits for `rerank`/`cross-encoder` across the repo.
  Supermemory ships `rerank: true` (+~100ms); mem0 ships five OSS rerankers.
- **No query rewriting.** Supermemory's `rewriteQuery` expands short queries
  with no extra cost.
- **The vector store is not actually pgvector.** `memory_embeddings.vector` is
  `BLOB NOT NULL` and cosine is computed in Python — there is no `vector` column
  type and no HNSW/IVFFlat index in `schema_ddl.py`, despite the
  README/ARCHITECTURE describing "PostgreSQL 17 + pgvector". So there is no ANN
  index; similarity is a linear scan.

**What is lost without it.** The five-signal RRF fusion (`hybrid_retriever.py`)
is genuinely good architecture — but four of its five signals are lexical
(keyword, TF-IDF cosine, hash-vector) or structural (graph, temporal). Only the
hash vector is _called_ "vector". A user asking a paraphrased question — no
shared vocabulary with the stored memory — retrieves poorly. This is the
mechanism behind the most common memory-system failure: "it stored the right
thing but surfaced the wrong thing." Every sophisticated subsystem downstream
inherits that ceiling.

**Fix (high impact, moderate cost).** Implement the `Embedder` protocol against
a real model and register it. The protocol already exists and
`embedding_validation.py` already dimension-checks, so this is a contained
change:

- Add providers (`fastembed`/`sentence-transformers` local, OpenAI, Ollama)
  behind `VALID_PROVIDERS`.
- Store a `provider`+`dimension` per row (already in the schema PK) so
  mixed/upgraded embeddings coexist.
- Add pgvector:
  `ALTER TABLE memory_embeddings ADD COLUMN embedding vector(768)` + HNSW index,
  backfill, and keep the BLOB path for SQLite tests.
- Add a reranker stage after RRF fusion, default off, with mem0-style
  `explain=True` score breakdowns (mem0's `score_details` is a real
  debuggability win Foresight lacks).

### 3.2 Ingestion is text-only; competitors ingest the world

**Finding.** The `documents`/`document_chunks` layer is a real asset — it tracks
source, `content_hash`, character spans, and supports re-derivation — but its
two extraction paths are heuristic paragraph chunking and LLM text extraction
(`document_layer.py`). There is no image, audio, video, PDF, or OCR path
anywhere (the earlier `ocr` match was "So**cr**atic").

**What is lost without it.** Supermemory ingests PDF (with OCR), Office, images
(visual + diagram interpretation), audio/video (transcription + speaker
detection), and AST-aware code chunking, plus connectors for Drive, Gmail,
Notion, OneDrive, GitHub, S3, and a web crawler. Foresight cannot absorb a PDF,
a screenshot, a meeting recording, or a codebase. For a system explicitly built
for clinical/sensitive work, "cannot ingest a scanned document" is a functional
disqualifier.

**Fix (staged).**

1. Add a `file` ingest path that extracts text then reuses the existing
   document→chunk→memory pipeline. Start with PDF (`pypdf`) +
   plain-text/Markdown/code; OCR (`tesseract`/`ocrmypdf`) second.
2. Add **type-aware chunking** — reuse `code-chunk`-style AST splitting for
   code, heading hierarchy for Markdown, semantic sections for PDF. This is
   cheap and immediately improves extraction quality.
3. Add a connector interface (`Connector` protocol, `connections` table, sync
   cursor, webhook receiver) _before_ writing any specific connector. Cloud-only
   in both competitors — a self-hostable connector is an unclaimed position.

### 3.3 There is no HTTP API — the semantics are unreachable from most stacks

**Finding.** The server is FastMCP with starlette custom routes for `/health`
and a local `/ui/*` dashboard. There is no REST API and no OpenAI-compatible
endpoint. The TypeScript CLI does not call an API — it reads Postgres directly
(`ARCHITECTURE.md`).

**What is lost without it.** mem0 and Supermemory both expose REST + Python/JS
SDKs, batch endpoints, and webhooks. Foresight requires the caller to be an MCP
client or to speak Postgres. That excludes every non-MCP framework (LangChain,
CrewAI, Vercel AI SDK, plain HTTP services), makes writes from webhooks and
connectors awkward, and forces the TS CLI into a direct-DB coupling that is a
security and schema-coupling liability.

**Fix.** Add a thin REST layer over the existing tool handlers — they are
already pydantic-validated at the boundary, so this is mostly routing and auth.
Priorities: `POST /memories`, `POST /search`, `POST /inject`, `POST /documents`,
`GET /profile`, batch endpoints. Add an OpenAI-compatible memory tool endpoint
only if it is cheap; REST is the unlock.

### 3.4 Typed edges exist in schema but not in behavior

**Finding.** This is the subtlest and most important gap, because it looks
solved from the schema:

- The `relationship_type` enum exists (`schema_ddl.py:152`).
- `capture.py` writes `derives` edges (lines 507, 595).
- `link_memories` lets an agent create
  `updates`/`extends`/`derives`/`contradicts`/`supports`/`related`.

But:

- **No automatic `updates` detection at write time.** Nothing decides that a new
  memory supersedes an old one and marks it.
- **No truth-enforcement in retrieval.** There is no `is_latest`/superseded
  flag, and the retriever's graph signal is _entity-based expansion_
  (`hybrid_retriever.py` docstring) — I found no evidence it consumes
  `memory_relationships` to demote superseded facts. `superseded` appears only
  in a proof-benchmark string literal.
- **No inference governance.** No `is_inference` flag and no review queue for
  `derives` facts.

**What is lost without it.** The system can _record_ that fact B updates fact A
and still let fact A win the ranking. That is worse than not having the edge,
because it creates the impression of contradiction handling without the
behavior. And ungoverned `derives` edges let the system infer something the user
never said and then treat it as fact — the exact failure Supermemory's
down-weighting + review queue prevents.

**Fix (highest leverage per line of code).**

1. Add `is_latest BOOLEAN DEFAULT true`, `inferred BOOLEAN DEFAULT false`, and
   `superseded_by` to `memories`; add `review_status` to metadata.
2. On write, run a cheap contradiction/supersede check against the top-K
   semantically similar memories (reuse `enhanced_synthesizer`'s contradiction
   detection, already built, `enhanced_synthesizer.py`): write an `updates` edge
   and set `is_latest=false` on the older memory.
3. In `hybrid_retriever`, apply a **ranking penalty** to `is_latest=false` and
   `inferred=true` rather than filtering — reusing the existing
   decay/deca-multiplier plumbing. Never hard-filter; mem0's lesson is that
   over-filtering loses recall.
4. Add `manage_inferred` actions (`list`, `approve`, `decline`, `undo`)
   following Supermemory's semantics. This is the single most defensible _novel_
   feature Foresight could ship, because neither competitor does it well
   automatically.

### 3.5 Smaller, concrete gaps

| Gap                                 | Evidence                                                                                                                                                                          | What is lost                                                                                                                                        |
| ----------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| **No memory expiration / TTL**      | `expires_at` exists only on `auth_sessions`; no `expires_at`/`forget_after` on `memories`                                                                                         | Temporary facts ("standup at 3pm today") live forever and pollute injection budget. Both competitors support it (`expiration_date`, `forgetAfter`). |
| **No org/project hierarchy**        | `tenants` + `users` only                                                                                                                                                          | Cannot model org → project → member the way mem0 Platform and Supermemory do. Blocks team/enterprise sales.                                         |
| **Profile built but not exposed**   | `synthesize_profile` defined at `server.py:5823` but **not decorated `@mcp.tool`**; only the CLI (`foresight_cli/commands/analysis.py:72`) and the `user_profile` prompt reach it | Agents cannot call the profile. This is a one-line fix with outsized value — Supermemory treats profiles as core.                                   |
| **No profile buckets**              | `profile_synthesizer.py` has static/dynamic only                                                                                                                                  | No third topical axis (preferences/goals/work). Cheap to add given the layers already exist.                                                        |
| **No async write status**           | writes are synchronous (background threads exist but no job handle returned)                                                                                                      | Callers cannot fire-and-forget and poll. mem0 returns `PENDING`+`event_id`; Supermemory returns `status: queued→done`.                              |
| **No batch operations**             | single-memory calls only                                                                                                                                                          | mem0 does 1000/call; bulk ingest is impractical.                                                                                                    |
| **No soft-delete recovery surface** | `archived_at` exists on `memories`                                                                                                                                                | Archive is present but there is no `include_archived` / restore semantics exposed equivalent to Supermemory's `include.forgottenMemories`.          |
| **No published benchmark numbers**  | `eval_harness.py` + `proof_benchmark.py` are internal                                                                                                                             | Cannot make a credibility claim. mem0 publishes (Platform-only) numbers; Supermemory publishes none but ships MemoryBench.                          |
| **No agent self-signup**            | —                                                                                                                                                                                 | mem0's `mem0 init --agent` lets an agent provision itself in <30s. Relevant to Foresight's agent-first positioning.                                 |
| **No query rewriting** _(shipped: `foresight/query_rewrite.py`, wired into `HybridRetriever.search` + `SearchOptions`/`POST /search`)_ | Was 0 hits | Short queries retrieved poorly. Supermemory merges multiple LLM rewrites at query-time cost; Foresight instead runs deterministic question→statement + content-core variants (original always first, per-signal merge by best rank, variant-count tie-break) with an opt-in `FORESIGHT_QUERY_REWRITE=llm` paraphrase that degrades silently. |

---

## 4. Recommended roadmap

**Tier 1 — retrieval credibility (do first; everything else is capped by this)**

1. Real `Embedder` providers behind `VALID_PROVIDERS` (local fastembed default
   to preserve the no-egress guarantee; OpenAI/Ollama opt-in).
2. pgvector column + HNSW index; keep BLOB path for SQLite tests.
3. Reranker stage after RRF, default off, with `score_details` explain output.
4. `is_latest` / `inferred` + supersede detection on write + ranking penalties
   in the retriever.
5. `manage_inferred` review queue (`list`/`approve`/`decline`/`undo`).

**Tier 2 — reach and breadth** 6. REST API over existing handlers (`/memories`,
`/search`, `/inject`, `/documents`, `/profile`, batch). 7. Memory expiration/TTL
(`expires_at`, hidden from search unless requested). 8. Expose
`synthesize_profile` as an MCP tool; add profile buckets. 9. Document ingestion:
PDF + Markdown/code + type-aware chunking, then OCR.

**Tier 3 — differentiation and scale** 10. Connector interface + first connector
(local filesystem/S3 are the self-hostable differentiators). 11. Async write +
status polling; batch endpoints. 12. Publish LoCoMo/LongMemEval numbers using
the existing eval harness — report **accuracy, tokens/query, and latency at a
fixed retrieval budget**, with a declared judge. This is the credibility move
neither competitor can fully copy, because both gate their best features to the
cloud.

---

## 5. The strategic read

mem0 and Supermemory are converging on the same conclusion: **stop reconciling
at write time, keep everything, and resolve truth asynchronously and
reversibly.** mem0 does it with an ADD-only extractor plus ranking-time decay
plus Dream. Supermemory does it with typed edges plus inference flagging plus a
review queue.

Foresight already owns the hardest parts of that convergence — a real decay
model, typed edges, curation runs, context blocks, an audit trail, encryption,
and tenant isolation enforced by tests. What it lacks is not memory _thinking_;
it is the **embedding, ingestion, and HTTP plumbing** that makes the thinking
visible to a caller.

The one thing both competitors gave up and Foresight still holds: **fully
self-hostable memory with no feature paywall.** mem0's graph, decay, temporal
reasoning, Dream, webhooks, and profiles are Platform-only, and its published
numbers come from managed infrastructure. Supermemory's connectors and MCP are
cloud-only and it publishes no numbers at all.

So the highest-value move is not to imitate their cloud features. It is to close
the retrieval-quality gap, add an HTTP surface, add real ingestion, and then
**publish reproducible benchmarks for the self-hosted build** — the position
neither competitor can occupy without cannibalizing its cloud revenue.

---

# Part II — The wider landscape (2026-11-10)

Sections 0–5 compared Foresight against mem0 and Supermemory. This part widens
the net to the rest of the AI-memory field — **Zep/Graphiti, Hindsight
(Vectorize), Letta, LangMem, and Cognee** (Membase as honorable mention) — and
records what each does that Foresight does not, plus new product directions.

**Status note on Part I:** several Tier-1/Tier-2 gaps have since shipped —
real embedder providers (`local-hash`, `fastembed`, OpenAI;
`semantic_search.py:84`), pgvector + HNSW (`schema_ddl.py:491-504`), a
fastembed cross-encoder reranker (`reranker.py`, `FORESIGHT_RERANK_ENABLED`),
truth-resolution columns (`is_latest`, `inferred`, `superseded_by`,
`review_status`; `schema_ddl.py:473-484`), a REST API (`rest_api.py`), and
bitemporal validity intervals (`valid_from`/`valid_to`, `schema_ddl.py:530-548`).
The remaining structural gaps are below.

---

## 6. The five additional competitors

### 6.1 Zep / Graphiti — bi-temporal facts done as a first-class query axis

Zep's engine (Graphiti, Apache-2.0) is a **bi-temporal knowledge graph**: every
fact carries `valid_from`/`valid_to` (when it was true in the world) plus
`observed`/`recorded` timestamps (provenance). Contradictions invalidate the
old fact rather than deleting it, so "what is true now?" and "what was true on
date X?" are both first-class queries. Its third layer, **Observations**, is the
interesting part: patterns consolidated from many episodes ("escalations
followed each of the last three IdP changes"), each carrying its supporting
evidence.

**vs Foresight:** Foresight now has `valid_from`/`valid_to` and `query_memories_temporal`
as_of queries, plus typed edges and an entity graph — the substrate is
comparable. What Zep has that Foresight lacks is (a) the **Observations layer**
as a distinct, evidence-linked memory type, and (b) contradiction-driven
*automatic* invalidation wired into retrieval (Foresight has `is_latest`
penalties; verify the write path actually closes validity windows, not just
flags).

### 6.2 Hindsight (Vectorize) — the most interesting memory *type system*

Hindsight (MIT, ~60k stars, launched Oct 2025) organizes memory into typed
channels that map loosely to cognition:

| Type                  | Meaning                                                        |
| --------------------- | -------------------------------------------------------------- |
| World facts           | Objective claims told to the bank ("Alice works at Google")    |
| Experience facts      | The bank's _own_ actions ("I recommended Python to Bob")       |
| Observations          | Consolidated beliefs from many facts, evidence-linked, refined-not-overwritten |
| Mental models         | Pre-computed answers for recurring questions, served instantly |
| Knowledge pages       | Living documents the bank maintains about itself/domains       |

Retrieval (`recall`) runs four parallel strategies — semantic, BM25, graph,
temporal — fused by **rank** (RRF, same as Foresight), then a cross-encoder
rerank, then **cut to a token budget, not top-k**. A `reflect` operation runs
an autonomous reasoning loop and returns a synthesized answer rather than a
fact list. It also ships open-weight memory-specific models (decision, rerank,
embedding) and configurable **disposition traits** (skepticism, literalism,
empathy) that influence belief formation.

**vs Foresight:** the retrieval stack is now nearly identical in shape
(hybrid RRF + reranker + injection budget). The gaps are the **epistemic
distinction** (world vs experience facts — Foresight memories don't record
who/what is the subject of a claim), **mental models / knowledge pages**
(cached synthesized answers), the **`reflect` operation**, and **disposition
traits** — where Foresight has a surprise asset: it already computes empathy
metrics and runs crisis detection (`memory_components.py`, `crisis_detection.py`),
so a disposition layer is closer than it looks.

### 6.3 Letta — the agent edits its own memory, with budgets

Letta (MemGPT lineage) treats memory as **context the agent manages itself**:

- **Memory blocks**: labeled, described, char-budgeted sections always pinned in
  context (persona, human, custom). Agents rewrite them via tools; the block's
  `description` field teaches the agent when and how to use it.
- **Archival memory**: agent-immutable long-term store, searchable via tools.
- **Recall memory**: full conversation history searchable on demand — distinct
  from curated facts.
- **Sleep-time agents**: background agents that consolidate and rewrite memory
  while the primary agent is idle.
- **Shared blocks**: multiple agents can attach to the same block; update once,
  visible everywhere.

**vs Foresight:** `manage_context_blocks` + `capture_in_flight_memory` cover the
mechanics, and `reflection_engine`/curation runs cover background consolidation.
Gaps: context blocks have **no description field or per-block char budget**
(the schema stores label+content only), there is **no recall-vs-archival
distinction** exposed as separate tools (raw events exist but aren't a
first-class `conversation_search`), and **no shared blocks across agents** —
Foresight scopes strictly by `tenant_id`+`user_id` with no `agent_id`/project
dimension at all (`schema_ddl.py` has no such column).

### 6.4 LangMem — procedural memory and prompt optimization

LangMem (LangChain, MIT) is a library, not a service, but its memory taxonomy
is the cleanest articulation of a gap Foresight shares with everyone:

- **Semantic** — facts/preferences (Foresight ✓)
- **Episodic** — past interactions as few-shot learning examples (partial: raw
  events + session transcripts exist, but aren't surfaced as reusable
  "what worked last time" exemplars)
- **Procedural** — _rules that change agent behavior_, distilled into the
  system prompt; LangMem pairs this with a **prompt-optimization loop** that
  evaluates and refines instructions over time.

**vs Foresight:** Foresight's `session_patterns` context block is the seed of
procedural memory, but nothing closes the loop: patterns are stored, not
_evaluated_, and there is no mechanism that turns "the agent keeps making the
same mistake" into an enforced prompt rule with a measured effect. This is the
least-crowded whitespace in the whole landscape — nobody self-hosted does
procedural memory well.

### 6.5 Cognee — graph pipelines, ontology grounding, `memify`

Cognee (Apache-2.0 core) runs an ECL pipeline (`add` → `cognify` → `memify`)
with two ideas worth stealing:

- **Ontology grounding**: an optional RDF/OWL vocabulary validates extracted
  entities — `annotate` mode tags `ontology_valid`, `strict` mode drops
  ungrounded entities. Cheap precision lever on entity extraction.
- **`memify`**: a post-ingestion refinement pass that prunes stale nodes,
  strengthens frequently-traversed edges, reweights by usage signals, and adds
  derived facts — essentially curation runs pointed at the graph rather than
  just memories.
- Also: **code as a graph** (symbol/dependency extraction) and a delightfully
  simple four-verb API (`remember`/`recall`/`improve`/`forget`).

**vs Foresight:** `graph_edge_decay` + curation runs are close to `memify`;
the missing piece is **usage-signal edge reweighting** (Foresight decays by
time, not by retrieval traffic) and **ontology/domain-vocabulary grounding**
for the entity extractor.

**Honorable mention — Membase:** one memory shared across an app, its AI
features, and coding tools via MCP; profile-centric. Validates demand for a
first-class, exportable **user profile** artifact — which Foresight has
(`synthesize_profile`) but doesn't expose as a portable document.

---

## 7. What the field converged on (and where Foresight stands)

Across all seven systems the architecture has converged:

1. **Keep everything, resolve truth asynchronously** — ADD-only writes,
   supersede/validity windows, reversible consolidation. Foresight is aligned
   and, with curation runs, ahead on the human-in-the-loop variant.
2. **Hybrid retrieval + cross-encoder rerank + token budget** — now table
   stakes; Foresight has shipped all three.
3. **A derived "belief" layer above raw facts** — Zep Observations, Hindsight
   observations/mental models, Cognee derived facts. Everyone is building
   this; Foresight's context blocks are adjacent but not evidence-linked.
4. **Background consolidation** — sleep-time agents, dreaming, memify,
   background managers. Foresight has the machinery (reflection engine,
   curation runs) but not the productized persona.
5. **Epistemic typing** — who said it, whether it's inferred, whether it's the
   agent's own action. Only Hindsight and (partially) Foresight's
   `inferred` flag bother.

Foresight's remaining structural deficits versus the whole field: **no
`agent_id`/project/namespace dimension** (blocks multi-agent and team use —
the most common enterprise ask), **no procedural memory loop**, **no portable
profile artifact**, and **no published benchmark numbers**.

---

## 8. New spins — concrete proposals

Ordered by (user value × fit with existing subsystems). Each names the
competitor idea it generalizes.

### Tier A — high value, mostly assembly of existing parts

1. **`reflect` tool — memory-backed reasoning, not just retrieval.**
   A tool that takes a question, runs retrieval + synthesis, and returns an
   answer *with citations to the memories used*. `inject_context` returns a
   raw slice; `reflect` returns a conclusion. This is Hindsight's reflect and
   Zep's graph-query UX in one tool, and it demos Foresight's unique depth
   (decay + validity + relationships) better than raw search ever will.
   *Files:* new tool in `server.py`, reuse `enhanced_synthesizer.py`.

2. **Mental models / FAQ cache.** Detect repeated `reflect`/`inject` query
   patterns; cache the synthesized answer as a first-class memory type with
   its own decay curve. Doubles as a token-economics win — telemetry already
   tracks saved tokens, and cached reflections are the cheapest possible
   injection. *Generalizes Hindsight mental models + knowledge pages.*

3. **Evidence-linked observations.** Extend context blocks (or a new
   `observations` table) so consolidated patterns carry `supporting_memory_ids`
   + a proof count, and are *refined* on new evidence rather than overwritten —
   Hindsight's model, built on Foresight's existing curation synthesis.
   Lets `inject_context` say "the agent has escalated 3 of the last 4 times
   this came up" with receipts.

4. **Namespace dimension: `agent_id` / `project`.** Add optional scoping
   columns so multiple agents (or a user + their sub-agents) can share a
   tenant while keeping private lanes — plus _shared_ context blocks visible
   to all agents in a project (Letta's shared blocks). This is the single
   biggest market-expansion move: it converts Foresight from "one user's
   memory" to "a team's memory," which is what Membase/Zep/mem0-Platform all
   sell. Schema touch is contained (nullable columns + scoping predicates).

### Tier B — differentiation

5. **Procedural memory loop.** A curation-run type that distills recurring
   corrections/failed approaches into candidate prompt rules, A/B-measures
   them via the existing `eval_harness.py`, and promotes winners into a
   `procedural_rules` context block. This is LangMem's procedural memory +
   prompt optimization, self-hosted, with measurement — a capability no
   competitor offers end-to-end. *Highest novelty; highest effort.*

6. **World vs. experience facts.** Tag memories with epistemic source
   (`subject=user|agent|world`) at extraction time. Cheap schema + prompt
   change; enables trust weighting ("the agent _believes_ it recommended X"
   vs "the user _said* X") and better crisis/safety handling. *Generalizes
   Hindsight's fact typing; synergizes with `crisis_detection.py`.*

7. **Sleep-time agent persona.** Productize consolidation as a named,
   schedulable agent (`foresight sleep` / cron-driven curation profile) with
   its own narrow toolset — review sessions, consolidate observations, prune,
   write reflections. Letta's sleep-time agents + Zep's dreaming, but
   self-hosted and auditable via the existing audit trail. Mostly a
   packaging/UX exercise over `manage_curation_runs`.

8. **Ontology grounding for entity extraction.** Optional per-tenant
   vocabulary (start as a simple YAML/JSON schema of entity types + relations,
   RDF/OWL later) that validates/canonicalizes `entity_extractor.py` output —
   Cognee's annotate/strict modes. Improves graph quality and gives enterprise
   users a governance hook. *Cheap precision win.*

9. **Portable profile artifact.** Export `synthesize_profile` output as a
   signed, versioned document (Markdown/JSON) the user can inspect, diff, and
   carry across deployments — Membase's profile-as-product, but open. Also
   doubles as a GDPR-style data-portability answer, strengthening the
   privacy moat.

### Tier C — credibility

10. **Publish benchmarks.** Run `eval_harness.py`/`proof_benchmark.py` against
    LongMemEval + LoCoMo on the self-hosted build; publish accuracy, tokens/
    query, and latency at fixed retrieval budget with a declared judge. Only
    Hindsight and Zep publish numbers; mem0's are Platform-gated; Supermemory
    publishes none. This is still the cheapest credibility unlock available.

---

## 9. Recommended sequencing

- **Now:** #1 `reflect` (demo-able, differentiating), #4 namespace dimension
  (unlocks teams), #10 benchmarks (credibility).
- **Next:** #2 mental models, #3 evidence-linked observations, #9 portable
  profile.
- **Later:** #5 procedural loop (needs eval infra maturity), #6 epistemic
  typing, #7 sleep-time persona, #8 ontology grounding.

The through-line: the field has commoditized *retrieval* (everyone has
hybrid+rerank+budget now, including Foresight). The open frontier is the
**derived layer** — observations, mental models, procedural rules, and
reflective answers — where Foresight's decay model, curation runs, eval
harness, and audit trail are already the right foundation, and where the
self-hosted/privacy positioning is uncontested.

## 10. Derived-layer implementation status

- **#1 `reflect`** — shipped. `reflect(question)` runs hybrid retrieval with
  query rewriting, synthesizes an answer (LLM when `FORESIGHT_LLM_*` is
  configured, deterministic extractive fallback otherwise), and returns
  verifiable citations (`memory_id`, excerpt, score, why_relevant). Exposed
  as MCP tool and `POST /reflect`.
- **#2 mental models** — shipped. Repeated questions are memoized in the
  `mental_models` table keyed by normalized question text; cache hits skip
  retrieval entirely (`cached: true`, zero tokens). TTL via
  `FORESIGHT_MENTAL_MODEL_TTL` (default 30d), expiry swept by memory GC, and
  models citing a superseded/archived/deleted memory are marked stale and
  self-heal on next hit. Managed via `manage_mental_models` /
  `POST /mental-models`.

---

_Companion source: `agent-memory-feature-report.md` (mem0/Supermemory feature
inventory). Part II sources: getzep.com docs + Graphiti paper (arXiv
2501.13956), hindsight.vectorize.io docs + vectorize-io/hindsight repo,
Letta docs (memory blocks/archival/recall), langchain-ai/langmem repo +
docs, Cognee docs (ECL/cognify/memify/ontologies). Vendor benchmark numbers
remain unverified vendor claims._
