# Feature Report: mem0 vs Supermemory (for Foresight)

Research date: session-scoped. Method: 34 successful direct page fetches (Mintlify `.md` variants + `llms.txt` indexes). `web_search` was **unavailable** in this session (`DEEPSEEK_API_KEY` missing), so the mem0 arXiv paper body and independent third-party benchmarks could **not** be fetched — those claims are marked UNVERIFIED. All fetched web content treated as untrusted data.

**Verification key:** `[docs]` = stated in official docs · `[marketing]` = vendor claim on vendor page · `[UNVERIFIED]` = could not confirm.

---

## mem0

### Architecture & data model

Two products sharing one engine: **Mem0 Platform** (managed, `MemoryClient`/`AsyncMemoryClient`) and **Mem0 Open Source** (self-hosted, `Memory`/`AsyncMemory`). Platform is the vendor's recommended default ([introduction](https://docs.mem0.ai/introduction), [llms.txt](https://docs.mem0.ai/llms.txt)).

Three storage layers, each tuned to a lookup pattern ([core-concepts/how-it-works](https://docs.mem0.ai/core-concepts/how-it-works), [memory-evaluation](https://docs.mem0.ai/core-concepts/memory-evaluation)):

| Store | Contents | Purpose |
|---|---|---|
| Vector DB | memory text, embeddings, metadata (timestamps, hash, categories, `attributed_to`) | primary fact storage + semantic retrieval |
| Graph/Entity store | entities + embeddings + linked memory IDs | entity boost (Platform: also backs Graph Memory) |
| SQL DB | history log (ADD events) + rolling message window | audit trail + extraction dedup context |

The two pages disagree slightly on which store is the "source of truth" for facts (how-it-works says SQL; memory-evaluation says vector DB is "primary fact storage"). Worth noting as a doc inconsistency.

**Scoping identifiers:** `user_id`, `agent_id`, `run_id` on both; **`app_id` is Platform-only** for app/tenant separation ([platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss)). Distinguish **graph entities** (people/places/concepts extracted from text, become graph nodes) from **entity IDs** (the scoping keys) — the docs explicitly call these different concepts ([graph-memory](https://docs.mem0.ai/platform/features/graph-memory)).

**OSS defaults** ([open-source/overview](https://docs.mem0.ai/open-source/overview)):
- As library: LLM `gpt-5-mini`, embedder `text-embedding-3-small`, vector store local Qdrant at `/tmp/qdrant`, history SQLite `~/.mem0/history.db`, reranker disabled until configured.
- As self-hosted server (Docker Compose `server/`): Postgres + pgvector; bundled providers `openai`, `anthropic`, `gemini`.
- Claimed provider breadth: **25 vector stores, 18 LLM providers, 11 embedders** ([platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss)) — the `llms.txt` index actually lists ~28 vector-store pages, so 25 is a floor, not exact.

### Ingestion / extraction pipeline (LLM-driven add pipeline)

**The current v3 algorithm is single-pass ADD-only.** This is the single most important architectural fact for Foresight, and it *contradicts* the older publicly-known ADD/UPDATE/DELETE/NOOP design.

Six stages ([memory-evaluation](https://docs.mem0.ai/core-concepts/memory-evaluation)):

1. **Store New Memories** — conversation enters the pipeline asynchronously, after the agent responds.
2. **Context Lookup** — retrieve top-10 related existing memories as dedup context.
3. **Distill Memories** — *a single LLM call* producing ADD-only facts from input + context.
4. **Deduplicate + Embed** — hash-based dedup (**MD5**, exact duplicates only), then vectorize.
5. **Graph Memory (Entity Linking)** — extract entities (proper nouns, quoted text, compound noun phrases), link across memories.
6. **Temporal Reasoning** — a *separate* pass reading each new memory alongside the source conversation and its date, extracting temporal metadata: when the event occurred, whether it is ongoing or completed, timing precision, and memory type (**event, state, plan, preference, relationship, absence**). Runs independently and can be async so writes stay fast.

**What changed from v2 → v3** ([migration/oss-v2-to-v3](https://docs.mem0.ai/migration/oss-v2-to-v3)):
- Old algorithm used **two LLM calls**: one to extract candidate facts, one to decide **ADD / UPDATE / DELETE** actions against existing memories. New algorithm collapses to **one ADD-only call**. Rationale stated verbatim: "The model spends its capacity on understanding the input rather than diffing against existing state."
- `add()` **previously returned `ADD`, `UPDATE`, `DELETE` events; now returns `ADD` only**. (NOOP is not mentioned in any current doc I fetched — treat NOOP as a paper-era artifact, UNVERIFIED against v3.)
- `custom_fact_extraction_prompt` → renamed `custom_instructions`; `custom_update_memory_prompt` deprecated.
- Vendor-claimed effect: **+20 points on LoCoMo (71.4 → 91.6)**, **+26 on LongMemEval (67.8 → 93.4)**, extraction latency "roughly halved" ([migration/oss-v2-to-v3](https://docs.mem0.ai/migration/oss-v2-to-v3)).

The pipeline ASCII from the migration page:
```
Input conversation
    → Retrieve top-10 related existing memories (for deduplication context)
    → Single LLM call: extract all distinct new facts
    → Batch embed extracted memories
    → Hash-based deduplication (MD5, prevents exact duplicates)
    → Batch insert into vector store
    → Entity extraction (for entity matching)
```

**Add-surface parameters** ([memory-operations/add](https://docs.mem0.ai/core-concepts/memory-operations/add)):
- `infer=True` (default) extracts structured memories; **`infer=False` stores raw payload exactly as provided** — docs warn this admits duplicates and mixing modes can save the same fact twice.
- `expiration_date` (`YYYY-MM-DD`; `expirationDate` in JS). Expired memories hidden from `search`/`get_all` unless `show_expired`/`showExpired`; still returned by ID fetch.
- `metadata` for later filtering.
- **Platform automatic conversation context**: you send only new messages; Mem0 pulls earlier messages sharing `user_id` (+ `run_id`) and uses them as extraction context. Worked example: "He turned 5 today" → stored as *"User's dog Biscuit turned 5"*. Without the prior turn it can only store *"User's male pet turned 5"*. Previously gated behind `version="v2"`, now default and needs no config.
- Platform `add` returns `status: "PENDING"` with an `event_id`; poll `GET /v1/event/{event_id}/`.

### Retrieval

Four signals, fused (Platform) ([how-it-works](https://docs.mem0.ai/core-concepts/how-it-works), [memory-evaluation](https://docs.mem0.ai/core-concepts/memory-evaluation)):

| Signal | Mechanism | Best for |
|---|---|---|
| Semantic | vector similarity | conceptual questions |
| Keyword | normalized term matching via **BM25 with verb-form lemmatization** | names, IDs, exact facts |
| Entity | boosts memories linked to query entities | "what do we know about Alice?" |
| Temporal | query temporal intent classified **with no extra LLM call**, scored against write-time temporal metadata | "when did…", recency, current state |

**Critical constraint:** "BM25 is a boost signal, not a recall expander. Only semantic search results are candidates: BM25 and entity scores boost ranking but don't add new candidates" ([migration/oss-v2-to-v3](https://docs.mem0.ai/migration/oss-v2-to-v3)). The temporal score is likewise additive and "semantic relevance always dominates"; it never filters candidates out.

**OSS vs Platform retrieval** ([memory-operations/search](https://docs.mem0.ai/core-concepts/memory-operations/search)):
- Platform: managed reranker catalog via `rerank=True`, compound `AND`/`OR` filters with field-level access, request-level `threshold`/`top_k`.
- OSS: basic field filters extensible via Python hooks; reranker must be configured locally/third-party; **no graph memory**; entity boost from overlap alone.
- OSS graceful degradation ([migration/oss-v2-to-v3](https://docs.mem0.ai/migration/oss-v2-to-v3)): missing spaCy (`mem0ai[nlp]`) → no entity extraction and no BM25 lemmatization (semantic-only). Missing `fastembed` on Qdrant → **BM25 silently disabled** (Qdrant uses sparse/BM25 vectors alongside dense in the same collection; other stores use native FTS). Missing entity store → no entity boost. Semantic search always works.
- `explain=True` returns `score_details` per result: semantic score, normalized BM25 score, entity boost, raw combined score, maximum possible score, final score, threshold used.

**v3 default changes (breaking)** ([migration/oss-v2-to-v3](https://docs.mem0.ai/migration/oss-v2-to-v3)): `top_k` **100 → 20**; `threshold` **None → 0.1** (must be in `[0,1]`); `rerank` **True → False**; entity IDs move into `filters` for `search()`/`get_all()` (top-level kwargs now raise `ValueError`), while `add()`/`delete_all()` keep top-level kwargs. The public top-level `score` still ranges `[0,1]` but is computed differently — absolute values shift, so hard thresholds must be retuned.

**Filter fields:** Platform enforces a fixed allowlist — `user_id`, `agent_id`, `app_id`, `run_id`, `created_at`, `updated_at`, `timestamp`, `expiration_date`, `categories`, `metadata`, `keywords`, `memory_ids`, plus `AND`/`OR`/`NOT`; any other key is rejected with a **400**. OSS filters on **any** metadata key with no allowlist, and its filter layer accepts `eq, ne, gt, gte, lt, lte, in, nin, contains, icontains` — but what actually executes depends on the configured vector store ([platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss), [open-source/features/metadata-filtering](https://docs.mem0.ai/open-source/features/metadata-filtering)).

**Entity matching store:** v3 auto-creates a parallel collection `{your_collection}_entities`. Managed vector DBs with restricted permissions require pre-creating it with the same embedding dimensions ([migration/oss-v2-to-v3](https://docs.mem0.ai/migration/oss-v2-to-v3)).

**Rerankers (OSS):** Cohere Rerank, Sentence Transformer (local cross-encoder), HuggingFace, LLM Reranker, Zero Entropy; with config, optimization, and custom-prompt pages ([llms.txt](https://docs.mem0.ai/llms.txt)).

### Graph memory

**OSS: removed entirely in v3.** `enable_graph`/`enableGraph`, `graph_store`/`graphStore`, and all external graph-store drivers (**Neo4j, Memgraph, Kuzu, Apache AGE, Neptune**) were deleted — **~4000 lines**. The `relations` field on search results is no longer returned; sending `enable_graph=True` on non-paginated `get_all` only adds an empty `"relations": []` for backward compatibility ([migration/oss-v2-to-v3](https://docs.mem0.ai/migration/oss-v2-to-v3)).

**Platform: native, built-in, always on, all plans — no external graph database.** ([graph-memory](https://docs.mem0.ai/platform/features/graph-memory))
- Nodes = graph entities (people, places, organizations, products, concepts), each stored once and embedded so differently-phrased references can match.
- Connections = entity ↔ every memory mentioning it; two entities related when they co-occur.
- **Schema-free and untyped: it does NOT assign labeled relationships** ("it won't, for example, record a 'manages' edge from one person to another"); connections are inferred from co-occurrence, not declared.
- Affects **ranking only** — folded into the combined `score`; there is no separate graph payload to parse.
- **Graph view** (dashboard visualization) is **Pro and Enterprise only**; the graph's ranking effect is on all plans.

### API & developer surface

**Core methods (identical on both products):** `add`, `search`, `get`, `get_all`, `update`, `delete`, `delete_all`, `history(memory_id)` ([platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss)).

**Platform REST surface** ([llms.txt](https://docs.mem0.ai/llms.txt)): memory CRUD + `batch_update`/`batch_delete` (**1000 memories per call**), memory `history`, feedback, create/get memory export (async jobs), events (`GET /v1/events/`, `GET /v1/event/{id}/`), entities (users/agents/apps), **profiles API** (get profile, profile settings incl. JSON-Schema-driven shapes, generate profiles over a 10-sample or one entity, generation job), organizations + projects + members, **8 Dream endpoints**, webhooks CRUD. OpenAPI spec at `https://docs.mem0.ai/openapi.json`.

**SDKs:** Python `mem0ai` 2.x, TypeScript `mem0ai` 3.x, Python CLI `mem0-cli`, Node CLI `@mem0/cli` 0.2.x. Platform SDK now uses typed option classes (`AddMemoryOptions`, `SearchMemoryOptions`) and camelCase in TS. `MemoryClient(api_key, org_id, project_id)` → `MemoryClient(api_key)`.

**MCP:** hosted server at `https://mcp.mem0.ai`, requires a Platform API key ([llms.txt](https://docs.mem0.ai/llms.txt)).

**Agent plugins:** `integrations/agent-plugin-core/` is the single source for shared Python+TS memory behavior; `integrations/mem0-agent-plugin/` is the portable **Agent Plugins v1** package. Claude Code plugin is **v0.3.1**, installs as `mem0@mem0-plugins`, captures evidence via lifecycle hooks, extracts memories in a detached background worker, and exposes one local MCP tool `search_memories` plus six `/mem0:*` skills and `mem0:sidekick` (Claude Code only). The portable package provides search + skills but **no automatic capture**, and its remember skill cannot save a new memory on its own.

**Agent self-signup** ([platform/agent-signup](https://docs.mem0.ai/platform/agent-signup)): `mem0 init --agent --agent-caller <name>` mints an account + API key in <30s with no email/dashboard; key stored at `~/.mem0/config.json` mode `0600`; rate-limited to **5 signups per day per IP**; direct API calls return a bare `403` with no `Retry-After`. Claim with `mem0 init --email <email>` — **API key never changes**, all memories transfer. `mem0 identify <name>` backfills attribution.

### Hosted-platform-only features

From [platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss) and feature pages:

- **Graph Memory** (native) — OSS has none.
- **Memory Decay** — opt-in per project, **off by default**, v3 search only. Multiplies ranking score by a scaling factor in **0.3×–1.5×** (just-accessed ≈1.5×; idle weeks 0.4–0.6×; idle months ≈0.3× floor). It is **a soft bias, never a filter** — candidates are over-fetched at `top_k × 3` (floor 50) to give reordering room; threshold is applied *before* decay, so a returned public `score` can legitimately land slightly below your requested threshold. Tracks at most the **last 20 access timestamps** per memory. Legacy memories fall back to `event_date`, then `updated_at`. Enabled via `PATCH .../projects/{id}/ {"decay": true}`. OSS raises `"The decay parameter is not supported by the OSS Memory SDK."` ([memory-decay](https://docs.mem0.ai/platform/features/memory-decay))
- **Temporal Reasoning** — OSS not supported; both `timestamp` and `reference_date` (`referenceDate`) raise "not supported by the OSS Memory SDK." `reference_date` simulates searching at a specific instant ([temporal-reasoning](https://docs.mem0.ai/platform/features/temporal-reasoning)).
- **Dream** — background consolidation with three independent actions ([dream](https://docs.mem0.ai/platform/features/dream)):

| Action | Behavior | Availability |
|---|---|---|
| **Synthesis** | distills **pattern memories** from recurring threads, added *alongside* sources, idempotent, each links back to its evidence | **opt-in, Pro+** |
| **Supersede** | marks older contradicted fact `superseded`, links to replacement; not deleted, still returned by default badged as history | always on, all plans |
| **Merge** | folds a duplicate into a canonical memory; merged record hidden from reads by default | always on, all plans |

  Read modes: default = active + superseded, merged hidden; `latest_only=true` = active only; `include_merged=true` = everything. Synthesis needs **≥20 memories**, runs per-user on a schedule — **Pro every 7 days, Enterprise daily (configurable)** — with **up to ~24h** to appear, and only considers memories scoped to a **`user_id` alone** (any `agent_id`/`run_id`/`app_id` excludes them). Enabling Synthesis sets a **forward boundary** — no bulk reprocess of history. Nothing Dream does is destructive.
- **Custom categories** — per project or per `add` call; OSS `Memory.add()` has no `custom_categories` param.
- **Webhooks** — project-scoped HTTP callbacks on memory and ingest events.
- **Memory Export** — schema-driven structured export jobs (Pydantic schema) over filtered memories.
- **Batch operations** — `batch_update`/`batch_delete`, 1000/call; OSS must loop single-memory calls.
- **Feedback** — `feedback(memory_id, ...)` records `POSITIVE`, `NEGATIVE`, or `VERY_NEGATIVE`.
- **Summaries** — `get_summary(filters)` returns a generated summary over matching memories.
- **Profiles** — structured always-current user summary in one read. (platform-vs-oss hedges that the feature "is still being finalized internally.")
- **Org/project structure** — multi-org, multi-project, member roles; OSS has no org/project concept (single local config).
- **Project-wide event feed** — `GET /v1/events/`; OSS has only per-memory `history()`.
- **Multimodal input**, **memory expiration**, **reranking**, **`custom_instructions`** are on **both**.

### Integrations / ecosystem

Vendor claims **22 tools** ([introduction](https://docs.mem0.ai/introduction)). Full list from [llms.txt](https://docs.mem0.ai/llms.txt):

- **Agent frameworks:** LangChain, LangGraph, LangChain Tools, LlamaIndex, CrewAI, AutoGen, Agno, Camel AI, ChatDev, Hermes, Pi Agent, DeepSeek Harness, OpenAI Agents SDK, Google AI ADK, Mastra, OpenClaw, Vercel AI SDK, Vercel (Marketplace), Strands Agents (native `MemoryStore`).
- **AI coding tools:** Claude Code, Claude.ai (remote MCP connector), Cursor, Codex, Kimi Code, OpenCode (ten native SDK tools, 7 skills), Antigravity.
- **Voice/realtime:** LiveKit, Pipecat, ElevenLabs.
- **Cloud:** AWS Bedrock.
- **Developer tools/automation:** Dify, Flowise, n8n, Zapier, AgentOps, Respan, Raycast.
- **In-repo Claude Code skills:** `skills/mem0`, `skills/mem0-cli`, `skills/mem0-vercel-ai-sdk`.

### Claimed benchmarks

From [core-concepts/memory-evaluation](https://docs.mem0.ai/core-concepts/memory-evaluation) — **all self-reported, Platform-only**:

| Benchmark | Score | Avg tokens/query |
|---|---|---|
| **LoCoMo** | **92.5** | 6,956 |
| **LongMemEval** | **94.4** | 6,787 |
| **BEAM (1M)** | **64.1** | 6,719 |
| **BEAM (10M)** | **48.6** | 6,914 |

LoCoMo detail: single-hop 91.2, multi-hop 91.3, open-domain **72.7**, temporal 92.0.
LongMemEval detail: single-session user 98.6, assistant 98.2, preference 96.7, knowledge update 93.6, temporal reasoning 97.0, multi-session 88.0. The docs concede knowledge-update (93.6) "remains the hardest category for an additive, ADD-only architecture: older facts are preserved rather than overwritten, so semantically similar prior facts can still surface alongside newer ones."
BEAM 10M detail: preference_following 90.4, instruction_following 82.5, knowledge_update 75.0, information_extraction 56.3, summarization 46.9, abstention 40.0, contradiction_resolution 32.5, multi_session_reasoning 26.1, event_ordering 20.2, temporal_reasoning **16.3**.

Method caveats the vendor states itself: results use **single-pass retrieval (one call, one answer, no agentic loops) at a top_200 retrieval budget**; scores carry **±1 point CI due to judge inconsistency**; and — importantly — "**Scores reflect Mem0's managed platform**, which includes proprietary optimizations not available in the open-source SDK. Open-source users should expect directionally similar gains but not identical numbers." So OSS users should not expect these figures.

Evaluation harness is open-sourced at `github.com/mem0ai/memory-benchmarks` and supports both Cloud and self-hosted OSS backends. CLI options: `--backend oss|cloud`, `--top-k` (default **200**), `--top-k-cutoffs` (default `10,20,50,200`; BEAM default 100), `--answerer-model`, `--judge-model`, `--provider` (`openai`/`anthropic`/`azure`), `--judge-provider`, `--max-workers` (10), `--predict-only`, `--evaluate-only`, `--resume`.

Not fetched (web_search unavailable): the original **mem0 arXiv paper** (arXiv 2504.19413 candidate), the paper's stated 26% accuracy / 91% latency / 90% token-savings figures, and its ADD/UPDATE/DELETE/NOOP tool-call description. **UNVERIFIED** — and note that any paper-era UPDATE/DELETE/NOOP description is superseded by the v3 ADD-only algorithm documented above.

---

## Supermemory

### Architecture & data model ("dreaming", spaces/containers)

Two internal components ([concepts/how-it-works](https://supermemory.ai/docs/concepts/how-it-works)):
1. **Learning model** — "decides what and how to learn, what is important, when to forget, creating relations." Described as a **custom, post-trained model** ("a post-trained model" per [comparison](https://supermemory.ai/docs/overview/comparison)). `[marketing]`
2. **Temporal Vector-graph engine** — "Fact-based temporal graph that has Vector, FTS, and graph built in."

**Core data model: documents vs memories** ([graph-memory](https://supermemory.ai/docs/concepts/graph-memory)):

|  | Documents | Memories |
|---|---|---|
| What | raw input you send | facts extracted by Supermemory |
| Examples | PDF, chat log, Drive file, URL | "Alex is a PM at Stripe" |
| Role | source of truth for RAG/SuperRAG | personal + entity state over time |
| Lifecycle | you add/update/delete | graph updates, extends, derives, forgets |

**Isolation: `containerTag`.** A string namespace; each tag is **hashed into a dedicated vector namespace** so "there is no shared index to filter through, which is why isolation is strict rather than best-effort" ([container-tags](https://supermemory.ai/docs/concepts/container-tags)). Naming rules: **≤100 chars**, pattern **`^[a-zA-Z0-9_:-]+$`** (colons allowed to build hierarchical tags like `org:acme:user:john`). Adding with a new tag **auto-creates a "space"**; singular `containerTag` is current, plural `containerTags` is **deprecated** and the `/v4` API only accepts the singular.

**Document pipeline stages** ([how-it-works](https://supermemory.ai/docs/concepts/how-it-works)): `Queued → Extracting (text/OCR/transcription/page fetch) → Chunking → Embedding → Indexing → Done`.

**"Dreaming" — the distinct second phase.** Document status `done` means **only chunks are indexed**; memories (graph facts, updates, derives) come from a **second phase called dreaming**, where content passes through the memory model ([how-it-works](https://supermemory.ai/docs/concepts/how-it-works)):

| Mode | Default | Behavior | When |
|---|---|---|---|
| `dreaming: "dynamic"` | **yes** | related documents are **grouped so memories form from coherent units**, not isolated writes. Memory extraction may continue **after** `status: "done"`. | production agents, connectors, sessions |
| `dreaming: "instant"` | no | this document is dreamed **on its own, right away**; bills **one extra operation** per document | demos, quickstarts, benchmarks |

This is a genuinely different architecture from mem0: mem0 extracts facts synchronously as part of `add`; Supermemory batches extraction across related documents so the unit of extraction is a *coherent session*, not a document. Each document produces **three outputs** sharing one `containerTag`: **chunks** (grounding), **memories** (graph), **profile** (static + dynamic).

**Memory types** ([graph-memory](https://supermemory.ai/docs/concepts/graph-memory)): **facts** (persist until updated), **preferences** (strengthen with repetition), **episodes** (decay unless significant).

**Memory relationships** — three edge types:

- **`updates`** — new fact replaces what was true before *for search purposes*; `isLatest` keeps retrieval on the current fact without erasing the past. History can remain for audit.
- **`extends`** — adds detail without invalidating the old fact; both stay valid.
- **`derives`** — Supermemory **infers** a fact never stated in one place ("Alex is a PM at Stripe" + "Alex frequently discusses payments" → *"Alex likely works on Stripe's core payments product"*).

**Automatic forgetting** ([graph-memory](https://supermemory.ai/docs/concepts/graph-memory)): **time-based** (temporary facts drop after expiring — "exam tomorrow", "meeting at 3pm today"), **contradiction** (updates win for what's true now), **noise filtering** (casual chatter less likely to become durable memory).

**SuperRAG vs memory paths** ([concepts/super-rag](https://supermemory.ai/docs/concepts/super-rag)): `taskType: "memory"` (default) = chunks/embeds **and** extracts facts, updates profile, links graph. `taskType: "superrag"` = chunk/embed/index only; **skips fact extraction, profile updates, and graph linking entirely** and is priced at **5x cheaper per token** (`sm_superrag_text`/`sm_superrag_rich` at 20% of `sm_tokens_text`/`sm_tokens_rich`). SuperRAG content is retrievable via `searchMode: "documents"` but "will **never** surface as a memory, contribute to a user's profile, or connect into the knowledge graph."

### Ingestion (multimodal, connectors, SMFS)

**Two entry points** ([content-types](https://supermemory.ai/docs/concepts/content-types)): `client.add()` for text and URLs; `client.documents.uploadFile()` for binary (takes a **stream**, not base64; also accepts a web `File`, a `fetch` `Response`, or the SDK's `toFile` helper).

Content coverage:
- **Text/markdown/code** — code is chunked with **`code-chunk`** (open-source `supermemoryai/code-chunk`), AST-aware: functions/methods stay intact, classes chunked by method, imports grouped, comments attached.
- **URLs** — fetched and cleaned (strips nav/ads/boilerplate); powered by **Markdowner** (`md.dhr.wtf`).
- **PDF** — text, tables, headers, **OCR for scanned documents**.
- **Microsoft Office** — Word/Excel/PowerPoint; Google Workspace via the Drive connector.
- **Images** — PNG/JPG/JPEG/WebP/GIF; extracts OCR text, **visual descriptions, diagram interpretations**. `fileType` **and** `mimeType` are both **required** for images.
- **Audio/video** — MP3/WAV/M4A/MP4/WebM; transcription, **speaker detection**, topic segmentation. `fileType: "video"` required for video.
- **JSON/CSV** — stringified through `add()`.

**Limits:** text **1MB**, files **50MB**, URLs fetched content up to **10MB**. Text is chunked at **sentence level with a 2-sentence overlap**. Typical processing: text near-instant, PDFs 1–5s, images 2–10s, video 10s+, webpages 1–3s. An irrecoverable processing error causes the document to be **auto-deleted after 2 minutes** ([add-memories](https://supermemory.ai/docs/ingestion/add-memories)).

**Chunking strategies** are type-aware ([super-rag](https://supermemory.ai/docs/concepts/super-rag)): PDFs/DOCX by **semantic sections** (headers, paragraphs, logical boundaries); code by AST; web pages by article structure; markdown by heading hierarchy.

**`customId`** drives updates and dedup. Two supported update patterns ([add-memories](https://supermemory.ai/docs/ingestion/add-memories)): send **only the new content** with the same `customId`, or send the **full updated content** — "Supermemory detects the diff and only processes new parts." `customId` also drives **diff billing** (full discount on already-seen tokens).

**`entityContext`** — a per-container context prompt steering extraction, **max 1500 chars**, persists on the container tag, combines with org-level filter prompts.

**`filterByMetadata`** — filtered writes: scopes which *existing* memories are used as context during ingestion. Scalar values match exactly, array values match if **any** element matches (OR), multiple keys combine with **AND**. The new document's own metadata is written normally; only context selection is filtered.

**Connectors** ([connectors/overview](https://supermemory.ai/docs/connectors/overview) via [llms.txt](https://supermemory.ai/docs/llms.txt)): **Google Drive, Gmail** (real-time Pub/Sub webhooks + incremental sync), **Notion** (real-time webhooks + workspace integration), **OneDrive** (scheduled sync, business accounts), **GitHub** (repository docs files; the only provider with resource management), **S3 / S3-compatible**, **Granola** (AI meeting notes/transcripts), **Web Crawler** (scheduled recrawling, robots.txt compliance). Plus connector troubleshooting and resource-management pages. **Connectors are cloud-only** — the self-hosted table marks them absent ([self-hosting/overview](https://supermemory.ai/docs/self-hosting/overview)).

**SMFS (Supermemory File System)** ([smfs/overview](https://supermemory.ai/docs/smfs/overview)) — "Memory your agent can grep." Mounts a container as a real directory so agents use `ls`/`cat`/`grep`/`find`/pipes. Four properties avoid the filesystem tax:
- **Semantic `grep` by default** — one call surfaces what matters across the container ranked by meaning; passing any flag falls through to real `grep` for exact matches.
- **Memory paths get distilled** — files marked as memory paths are extracted and indexed, so they don't bloat context.
- **Virtual `profile.md`** at the mount root — a live container digest the model can `cat` instead of walking the tree.
- **Bidirectional background sync** — local reads hit cache; writes push to Supermemory.

Two deployment shapes: the **`smfs` binary mount** (NFSv3 on macOS, FUSE on Linux) for real-filesystem agents, and the **bash tool** (`@supermemory/bash` for TS, `supermemory-bash` for Python) for serverless/edge (Cloudflare Workers, AWS Lambda, Vercel, Modal). Provider guides exist for **Daytona, E2B, Vercel AI SDK, Cloudflare**. SMFS is open source and free.

**Historical backfill** ([batch-ingest-historical-data](https://supermemory.ai/docs/ingestion/batch-ingest-historical-data)) supports `documentDate` and stable custom IDs via a batch ingestion API.

### Retrieval

Single `search` call, three modes ([recall/search](https://supermemory.ai/docs/recall/search)):
- **`hybrid`** (vendor-recommended) — memories **and** document chunks together.
- **`memories`** (the documented default) — extracted memories only.
- **`documents`** — raw chunks only, skipping extracted memories.

Parameters: `q`; `containerTag`; `limit` (**default 10**); `threshold` (**default 0.5**, 0–1 similarity cutoff); `rerank` (**default false**, cross-encoder re-scoring, **+~100ms**); `rewriteQuery` (**default false** — generates multiple rewrites, searches all, merges results; "No extra cost, but adds latency"); `filters` (`AND`/`OR`); `include` (**default off**) = `{ documents, summaries, relatedMemories, forgottenMemories }`.

Response shape: `results[]` each carrying **either** `memory` (extracted fact) **or** `chunk` (document content), plus `similarity`, `metadata`, `updatedAt`, `version`; envelope has `timing` (ms) and `total`.

Filter types: string equality; `string_contains`; `numeric` with `numericOperator` (e.g. `>=`); `array_contains`; `negate: true`.

**Graph-aware retrieval:** `include: { relatedMemories: true }` returns graph edges inline — `context.parents[]` / `context.children[]` with a `relation` field (`extends`, `derives`). Documented worked example resolves an entity chain never stated in one sentence: gift → VP of Product → Sarah → Tokyo offsite, with `timing: 287` ms in the sample payload.

**Query optimization:** `rerank: true` for complex/technical queries where precision beats speed; `rewriteQuery: true` for short queries where recall matters (expands "how to auth" → "authentication login oauth jwt…").

**Forgotten-memory recovery:** search **excludes** forgotten memories and memories past their `forgetAfter` expiry by default; `include.forgottenMemories: true` recovers them.

**Performance claim:** retrieval latency target **~sub-300ms p50** on the managed platform for typical search workloads ([security](https://supermemory.ai/docs/overview/security)) `[marketing]`.

**TypeScript SDK note:** call `client.search({ q, searchMode })` directly; `client.search.memories()` and `client.search.documents()` are **deprecated in TS** (no migration required). The **Python SDK is unaffected** — `client.search.memories()` remains the call there.

### Graph memory

The graph is the core differentiator, described as "a **living knowledge graph of facts on top of other facts** — not a static folder of embeddings, and not classic entity–relation–entity triples you maintain by hand" ([graph-memory](https://supermemory.ai/docs/concepts/graph-memory)).

Rules stated: (1) **memories are atomic** — each has enough information and context about one topic; (2) **they always build on each other** — with `updates` the model knows the history, a memory `extends` from others, and new facts `derives` from existing knowledge.

**Key contrast with mem0's graph:** mem0's Platform graph is **untyped co-occurrence** and affects ranking only. Supermemory's graph has **three named, typed edge semantics** (`updates`/`extends`/`derives`) that change what is retrievable as current truth, and is directly exposed to the caller via `include.relatedMemories`. Both are schema-free in the sense that the developer declares nothing.

**Inferred-memory governance** ([recall/memory-review](https://supermemory.ai/docs/recall/memory-review)) — `derives` facts are guesses, flagged **`isInference: true`** and **down-weighted in search** until confirmed. A review queue exists:
- `GET /v3/container-tags/{containerTag}/inferred` — returns up to **50** memories, ordered by **`parentCount` descending** (most strongly supported first) then `createdAt` descending; excludes forgotten, expired, or already-reviewed. Unknown/empty tag returns `{memories: [], total: 0}`.
- `POST /v3/container-tags/{containerTag}/inferred/{memoryId}/review` with `action: approve | decline | undo`. **Approve** clears `isInference` (ranks like a stated fact); **decline** sets `isForgotten` (leaves search and queue); **undo** restores `isInference: true`, un-forgets, clears the stamp. The reject action is named **`decline`** — there is no `reject`. `409` if not reviewable.

Every reviewed memory is stamped with `reviewStatus` in metadata.

### User profiles

Profiles are "**automatically maintained collections of facts about your users**" built from all their interactions — a persistent "about me" document, one per `containerTag` ([concepts/user-profiles](https://supermemory.ai/docs/concepts/user-profiles)).

**Three axes:**
1. **Static** — long-term stable facts ("Sarah is a senior software engineer at TechCorp", "prefers technical docs over video").
2. **Dynamic** — recent context and temporary states ("migrating the payment service to microservices", "debugging a memory leak in auth service").
3. **Buckets** — a **third, independent axis splitting facts by topic**, developer-defined (`preferences`, `goals`, `work`). Every org starts with a default `preferences` bucket. Buckets can be **org-level** or **per-space** — and space buckets are **add-only**, so a container tag always keeps every org-level bucket. A classifier sorts each fact into matching buckets as content is ingested. Bucket **descriptions steer the classifier** ("explicit first-person preferences only, exclude inferred traits" produces cleaner buckets). Separate from `filterPrompt`, which controls what gets ingested at all. `GET` profile with `include: ["buckets"]`, `buckets: [...]`. An endpoint suggests **3–6 bucket definitions** from an org context prompt. ([profile-buckets](https://supermemory.ai/docs/user-profiles/buckets))

**The stated rationale for profiles** is the "non-literal-matching" problem: search retrieves what's *similar to the query*, so facts that should be known **regardless of the query** — the user's name, pronouns, timezone, tone preferences, role — will never surface. The docs' worked example: "call me Dhravya, not my full name" told once during onboarding has near-zero vector similarity to "help me plan a trip to Japan", so search correctly omits it, but it is **always in `profile.static`**.

**Vendor-claimed economics** `[marketing]`: profiles cost **1 call / 50–100ms** vs **3–5 queries / 200–500ms** for a search-only architecture, because a profile "rides along with every prompt for free" rather than paying a per-turn `search(prompt)` round trip.

**Profiles support the same metadata filtering as search** ([filtering](https://supermemory.ai/docs/concepts/filtering)): any `AND`/`OR` filter also narrows which memories are eligible to contribute to `static`, `dynamic`, and `buckets`. Use case given: a support agent that should only see profile facts derived from support tickets, not from an internal wiki synced into the same container.

Update mechanics: ingest → extract facts → add/update/remove profile facts → always current; no manual profile management.

### API & developer surface

**REST endpoints observed** (from [llms.txt](https://supermemory.ai/docs/llms.txt) and page bodies):
- **Ingest:** `POST /v3/documents` (add), `POST /v3/documents/file` (upload), batch add documents, ingest-or-update conversation, update document, delete by id/customId, bulk delete.
- **Recall:** `POST /v4/search`, `POST /v4/profile`.
- **Memories (v4):** `POST /v4/memories` (create directly), `PATCH /v4/memories` (update, versioned), `DELETE /v4/memories` (forget), `POST /v4/memories/forget-matching`.
- **Documents:** list, get, get processing documents (`view=active|pending|all`), update, delete, get chunks, get presigned file URL (**24h** time-limited).
- **Container tags:** get/update settings, delete (owner/admin only), merge container tags + merge status.
- **Connections:** create, list, get by id/provider, configure, fetch resources, sync, list documents, delete.
- **Settings:** get/update org settings, suggest profile buckets, reset organization data (removes documents, memories, spaces, connections, org settings — preserves org, members, billing).
- OpenAPI: `https://api.supermemory.ai/v4/openapi`.

**Create Memories directly** ([recall/memory-operations](https://supermemory.ai/docs/recall/memory-operations)) — bypass document ingestion entirely; "Generates embeddings and makes them immediately searchable." Useful when you already know the exact facts. **1–100 items** per call, `content` max **10,000 chars**, optional **`isStatic: true`** for permanent identity traits (name, hometown), optional metadata (strings/numbers/booleans). Returns a lightweight **`documentId` for traceability** even though no ingestion ran.

**Forget** — soft delete, excluded from search but preserved with `isForgotten=true`. Identify by `id` or **exact `content`**, scoped to `containerTag`; optional `reason` recorded as `forgetReason`.

**Forget Matching (agentic mass-forget)** — bulk soft-delete two ways: a natural-language **`query`** (service semantically searches, an LLM decides which memories are genuinely about the target) or an explicit **`ids`** list. Controls: **`dryRun`** (preview `candidates`), **`threshold`** (default **0.5**), **`maxForget`** (**1–500, default 100**, ignored in id mode). Returns `forgetBatchId` tagged on every forgotten memory. Notable safety design: "Identity is server-owned: the LLM only ever references opaque handles for the memories a search returned, so it can never forget a memory outside the results it reviewed." Recommended pattern is dryRun → take `ids` → apply bound to exactly that set, since re-running by `query` can drift.

**Versioned update** — `PATCH` creates a new version; **original preserved with `isLatest=false`**.

**Auth & access control** ([authentication](https://supermemory.ai/docs/authentication), [container-tags](https://supermemory.ai/docs/container-tags)): org API keys and **container-scoped keys** (read or write permission per tag), plus connector branding. Member restrictions can limit a member to certain tags. Enforcement is at the data layer: requesting a tag outside the allowed set → **403**; write to a read-only tag → **403**; a restricted caller supplying no tag is auto-scoped to its allowed tag(s). Stated consequence: "you can hand out an API key that is physically incapable of reading or writing another tenant's data."

**Per-container settings:** `name` (display name) and **`entityContext`** (custom context prompt applied when processing documents in that container).

**SDKs:** official Python and JavaScript ([supermemory-sdk](https://supermemory.ai/docs/integrations/supermemory-sdk)).

**MCP:** Supermemory MCP with **OAuth**, shared memory, team spaces, interactive workflows; setup guides for **ChatGPT Web** and **Claude Desktop** ([supermemory-mcp/mcp](https://supermemory.ai/docs/supermemory-mcp/mcp), [setup](https://supermemory.ai/docs/supermemory-mcp/setup)). Cloud-only.

**Coding-agent plugins:** Claude Code, Codex, OpenCode, Cursor, OpenClaw (Telegram/WhatsApp/Discord/Slack), Hermes, Muse Code, Grok Bot. Also a dedicated **Agents, skills and MCP** page covering CLI, skill, and docs MCP.

**Framework integrations:** Vercel AI SDK (`withSupermemory` with `mode: "full"` = profile + query search), OpenAI SDK, OpenAI Agents SDK, LangGraph, Microsoft Agent Framework, Mastra, VoltAgent, Convex, LangChain, CrewAI, Agno, Pipecat, Cartesia, n8n, viaSocket, Zapier, Eve, **Claude Memory Tool** (native Anthropic memory tool with Supermemory as backend), and an interactive **Memory Graph** visualization.

**Observability:** an **Analytics & Monitoring** page covers usage, errors, and logs.

**MemoryBench** ([memorybench/overview](https://supermemory.ai/docs/memorybench/overview)) — open-source benchmarking framework, **MIT licensed**, `github.com/supermemoryai/memorybench`. Fixed pipeline **`INGEST → SEARCH → ANSWER → EVALUATE → REPORT`**, **checkpointed per phase** so a long run resumes from the last completed step. Benchmarks: **LoCoMo, LongMemEval, ConvoMem**. Providers: **Supermemory, Mem0, Zep**. **Judge-agnostic** (GPT-4o, Claude, Gemini, or any configured model) explicitly so "results aren't an artifact of one evaluator's bias." Ships a **Claude Code skill** (`/memorybench`) that analyzes your memory code, generates a provider adapter, registers it, runs the benchmark against chosen competitors, and reports **accuracy, latency, and context-token** results side by side. Scored via **MemScore** (qualitative judging + quantitative metrics).

### Deployment options

|  | Self-hosted | Platform |
|---|---|---|
| Full Memory API | ✅ | ✅ |
| Hybrid semantic search | ✅ | ✅ |
| Embeddings | local default, or OpenAI/Gemini/Ollama | same provider stack, managed |
| File ingestion (PDFs, images) | ✅ | ✅ |
| Connectors (Drive, Notion, Gmail, OneDrive) | — | ✅ |
| Supermemory MCP | — | ✅ |
| Memory extraction | your model, your key | proprietary long-horizon models |
| Infrastructure | your machine | globally distributed |

([self-hosting/overview](https://supermemory.ai/docs/self-hosting/overview))

- **Install:** `curl -fsSL https://supermemory.ai/install | bash` or `npx supermemory local`. **One binary, no Docker, no database to provision, no config files**, boots in seconds, open source (`git.new/memory`).
- **Zero config:** the **Supermemory graph engine embedded** (created automatically on first boot); **built-in local embeddings** default **`Xenova/bge-base-en-v1.5` (768d)** with no API key; an **API key generated and printed on first boot**.
- **Drop-in:** same API as cloud — change `baseURL` to `http://localhost:6767`. Coding plugins target it via `SUPERMEMORY_API_URL=http://localhost:6767` (Muse Code: `baseUrl` in `.muse/supermemory.json`, because hook env is cleared).
- **Fully offline:** works with any OpenAI-compatible endpoint — Ollama, LM Studio, vLLM, llama.cpp; docs suggest `gpt-oss-20b`. Local graph engine + local embeddings + local LLM.
- **Enterprise/dedicated** for stricter residency, air-gap, or custom deployment ([local-vs-enterprise](https://supermemory.ai/docs/self-hosting/local-vs-enterprise)).
- **Self-hosted caveat stated by the vendor:** the hosted platform's proprietary long-horizon models give "higher quality, cheaper at scale" than whatever model you point the self-hosted server at.
- **Billing** model: **SM tokens**, operations, search, SuperRAG, **diff billing**, plans, and programmatic usage APIs ([billing](https://supermemory.ai/docs/overview/billing)).

**Security & compliance** ([security](https://supermemory.ai/docs/overview/security)):
- **SOC 2 Type II — Certified** (advertised on Scale-and-above plans).
- **GDPR — Compliant**, with access and erasure workflows.
- **HIPAA — BAA available** for eligible cloud plans (Scale/Enterprise), **cloud-only unless self-hosted** under your own controls.
- Encryption: **TLS in transit**, **AES-256 class at rest** in managed cloud.
- **"Your customer content is never used to train models — this applies to every plan, free or paid."**
- GDPR-style erasure path: scope each end-user to a container tag, delete that container's content via API/console, revoke scoped keys. "Designing isolation up front makes erasure a single boundary operation instead of a forensic search."
- Connector note: OAuth connectors pull user-authorized content; disconnecting stops future sync but "you still control whether already-ingested documents remain in the memory store."

### Claimed benchmarks

**No numeric scores appear on any page I could fetch.** The docs make only qualitative claims:
- "It is the [state of the art] across multiple different benchmarks, like **LongMemEval** and **LoCoMo**. It's also the best in a lot of independantly run benchmarks, like the **SWEContext** bench." ([what-is-supermemory](https://supermemory.ai/docs/overview/what-is-supermemory)) `[marketing]`
- "**#1 on LongMemEval, LoCoMo, and ConvoMem**, plus independent benches like SWEContext."
- All three of those claims link to the same destination: `https://supermemory.ai/research` — i.e. **self-reported, no third-party citation inline**.
- The SWEContext citation is a bare PDF link, `https://arxiv.org/pdf/2602.08316`. That arXiv ID looks **malformed/implausible** (a `2602.*` identifier implies February 2026, and arXiv generally serves `/abs/` for citable references). **UNVERIFIED** — I could not fetch or confirm this paper.
- No accuracy, token-cost, or latency figures are published in the docs; the vendor instead directs readers to run **MemoryBench** themselves and links `supermemory.ai/research` for numbers. To their credit this is a reproduce-it-yourself posture rather than a number-drop, but it means **no Supermemory benchmark claim in this report is numerically verifiable from primary docs**.

---

## Evaluation methodology

How memory systems are actually benchmarked, and where the numbers come from.

**The three axes.** mem0's evaluation page states the framing crisply: "Evaluating a memory system at scale comes down to three parameters: **accuracy** (what the benchmarks measure), **cost** (context tokens per query), and **performance** (latency). Optimizing one is easy. Balancing all three at scale is the actual problem." The page's core argument is **token efficiency**: "Most AI agent memory systems retrieve information by maximizing context window size. That works on benchmarks but not in production, where every token adds cost." mem0 claims **under 7,000 tokens per retrieval call** while "full-context approaches on the same benchmarks routinely consume 25,000+ tokens per query" ([memory-evaluation](https://docs.mem0.ai/core-concepts/memory-evaluation)) `[marketing]`.

**The benchmarks:**

| Benchmark | Source | Scope |
|---|---|---|
| **LoCoMo** | [snap-research/locomo](https://github.com/snap-research/locomo) | fact recall across extended multi-session conversations — single-hop, multi-hop, open-domain, temporal (and adversarial); mem0's runner does "~300 questions across 10 conversations (fastest benchmark)" |
| **LongMemEval** | [xiaowu0162/LongMemEval](https://github.com/xiaowu0162/LongMemEval) | long-term memory across sessions incl. mid-conversation knowledge updates; 500 questions across 6 categories |
| **BEAM** | [mem0ai/memory-benchmarks](https://github.com/mem0ai/memory-benchmarks) | mem0's own; **1M and 10M token scales**, 10 task categories, 100 conversations. "the only public benchmark that operates at context volumes production AI agents actually encounter" |
| **ConvoMem** | [Salesforce/ConvoMem](https://huggingface.co/datasets/Salesforce/ConvoMem) | personalization, preference learning, and reference resolution within a conversation |

Note the asymmetry: **LoCoMo and LongMemEval are shared** (both vendors use them, so they are the only directly comparable axis). **BEAM is mem0-only**; **ConvoMem is in Supermemory's MemoryBench only**. Supermemory's MemoryBench also runs **Mem0** and **Zep** as providers, so it can generate cross-vendor numbers — but those are Supermemory-published runs.

**Methodological controls that matter:**
- **Retrieval budget.** mem0 reports at **top_200** (the 200 highest-ranked memories per query), configurable via `--top-k`. This is the single most important knob for comparability — a "95% using 25K tokens per query" result is not comparable to "90% using 7K tokens." mem0's own guidance: "Always compare systems using the same retrieval budget, the same model, and the same latency budget. A frontier model at maximum recall is not comparable to a smaller production-grade model at production-realistic retrieval depth."
- **Single-pass vs agentic.** mem0's headline numbers use "a single-pass retrieval setup (**one retrieval call, one answer, no agentic loops**)." Multi-turn agentic retrieval inflates accuracy.
- **Judge instability.** mem0 notes "**±1 point confidence interval due to judge inconsistency**." MemoryBench makes the judge **pluggable and judge-agnostic** specifically to avoid one evaluator's bias.
- **Ceiling effects.** mem0 flags that single-session categories are near-saturated (97%+) so gains there are less meaningful than temporal or multi-session gains.
- **Harness reproducibility.** Both vendors open-sourced their harnesses: mem0's `memory-benchmarks` (supports OSS and Cloud backends, resumable) and Supermemory's `memorybench` (MIT, checkpointed five-phase pipeline, plugin providers/benchmarks/judges).
- **Scale realism.** mem0's own strongest caveat is worth quoting for Foresight's methodology: "**Saturating a small benchmark is not the same as building a memory system that works at scale.** Small benchmarks can be brute-forced with aggressive retrieval and frontier models." Its BEAM 1M→10M drop (64.1 → 48.6 overall; temporal_reasoning 61.8 → **16.3**) is the clearest public evidence that small-benchmark scores overstate production capability.

**Concrete comparable numbers** (both self-reported):

| System | LoCoMo | LongMemEval | Tokens/query | Source |
|---|---|---|---|---|
| mem0 (Platform v3) | **92.5** | **94.4** | 6,956 / 6,787 | [mem0 docs](https://docs.mem0.ai/core-concepts/memory-evaluation) |
| mem0 (v2 → v3 delta) | 71.4 → **91.6** | 67.8 → **93.4** | — | [mem0 migration](https://docs.mem0.ai/migration/oss-v2-to-v3) |
| Supermemory | not published | not published | not published | [supermemory docs](https://supermemory.ai/docs/overview/what-is-supermemory) |

**Independent verification: not achieved in this session.** `web_search` was unavailable (missing `DEEPSEEK_API_KEY`), so I could not retrieve the mem0 arXiv paper, its original reported figures (commonly cited as ~26% higher accuracy over OpenAI Memory with ~91% lower p95 latency and ~90% token savings vs full-context), or any third-party head-to-head. **Treat every number in this section as a vendor claim**, and note mem0 explicitly states its numbers come from the **managed Platform** with "proprietary optimizations not available in the open-source SDK" — self-hosted OSS users should expect different results.

---

## Feature inventory table

| Capability | mem0 | Supermemory | Notes |
|---|---|---|---|
| Auto fact extraction | ✅ ADD-only, single LLM call (v3) | ✅ via "dreaming" second phase | mem0 extracts per `add`; SM batches related docs into coherent units |
| Extraction unit | conversation slice + top-10 related memories as context | coherent grouped documents (dynamic) | SM's `dreaming: dynamic` is the default and higher-quality mode |
| Dedup | ✅ hash-based (MD5), exact-match only | ✅ Merge relation + duplicate folding | mem0 dedup is exact-hash; SM's is semantic |
| Contradiction resolution | Partial — ADD-only keeps both; Platform `Dream` **Supersede** | ✅ `updates` edge, history preserved via `isLatest` | mem0 admits knowledge_update is its weakest LME category (93.6) |
| Temporal reasoning | ✅ Platform-only (event/state/plan/preference/relationship/absence metadata) | ✅ inherent to "temporal vector-graph engine" | mem0: `reference_date` param; OSS raises "not supported" |
| Decay / forgetting | ✅ **Memory Decay** (Platform, opt-in, 0.3×–1.5× soft bias) | ✅ automatic: time-based, contradiction, noise filtering | mem0's is a ranking bias that never filters; SM's actually drops facts |
| Background consolidation | ✅ **Dream** (Synthesis opt-in Pro+, Supersede + Merge always on) | ✅ dreaming pipeline + profile rebuild | mem0 Synthesis: ≥20 memories, 7-day (Pro) / daily (Ent) cadence, ≤24h delay |
| Graph memory | Platform-only, native, always on; **OSS removed in v3** (~4000 lines deleted) | ✅ core engine, always on | mem0: untyped co-occurrence, ranking-only, no `relations` payload |
| Graph edge types | ❌ none — explicitly no labeled relations | ✅ **`updates` / `extends` / `derives`** | Biggest architectural divergence |
| Graph visualization | ✅ Graph view (Pro/Enterprise only) | ✅ interactive Memory Graph integration | mem0 gates behind paid plans |
| Inferred-fact governance | ❌ none | ✅ `isInference` flag, down-weighted, review queue | SM: `GET .../inferred` (max 50, parentCount desc), review `approve`/`decline`/`undo` |
| External graph DB support | ❌ removed (was Neo4j/Memgraph/Kuzu/AGE/Neptune) | ❌ none — built-in only | Both converged on "no external graph store to provision" |
| Hybrid retrieval | ✅ semantic + BM25 + entity + temporal, rank-fused | ✅ semantic + FTS + graph (engine-internal) | mem0: BM25/entity **boost only, never add candidates** |
| Reranking | ✅ Platform managed catalog / OSS 5 providers (Cohere, Sentence-Transformer, HF, LLM, ZeroEntropy) | ✅ `rerank: true` cross-encoder (+~100ms) | Both default **off** |
| Query rewriting | ❌ not documented | ✅ `rewriteQuery: true`, multi-rewrite merge, no extra cost | |
| Score explainability | ✅ OSS `explain=True` → `score_details` (semantic, BM25, entity boost, max, final, threshold) | ❌ not exposed (internal only) | mem0's is a real debuggability advantage |
| Metadata filtering | ✅ Platform allowlist (unknown key → 400); OSS any key | ✅ `filters` AND/OR, 5 filter types | Opposing philosophies: mem0 restricts, SM permits |
| Configurable default thresholds | ✅ `threshold` 0.1 (v3), `top_k` 20 (v3) | ✅ `threshold` 0.5 default, `limit` 10 | Both changed defaults in recent majors |
| Multimodal ingest | ✅ images, PDFs | ✅ text, URLs, PDF+OCR, Office, images, audio, video, code, JSON/CSV | SM materially broader |
| OCR / transcription | Partial (images, PDFs) | ✅ PDF OCR, image visual+diagram interpretation, audio/video transcription + speaker detection | |
| Type-aware chunking | ❌ not documented | ✅ semantic sections (PDF), AST via `code-chunk`, headings (MD) | SM open-sourced `code-chunk` |
| Connectors (Drive/Notion/Gmail/S3…) | ❌ none | ✅ Drive, Gmail, Notion, OneDrive, GitHub, S3, Granola, Web Crawler | SM cloud-only |
| File-system abstraction | ❌ | ✅ **SMFS** — mount + semantic `grep` + virtual `profile.md` | SMFS free/open source; NFSv3/FUSE + serverless bash tools |
| Async writes | ✅ Platform `PENDING`+`event_id`; OSS `AsyncMemory` | ✅ `status: queued → done`, poll `documents.get()` | |
| Content-identity updates | ❌ no `customId` concept | ✅ `customId` (append or full-replace; diff-processed) | SM's `customId` also drives diff billing |
| Expiration / TTL | ✅ `expiration_date` (`show_expired` to include) | ✅ `forgetAfter` expiry + soft-forget | Different primitives: date field vs forget queue |
| Soft delete | ❌ delete is hard (though history log exists) | ✅ `isForgotten=true`, recoverable via `include.forgottenMemories` | |
| Versioned memory history | ✅ `history(memory_id)` + ADD event log | ✅ `PATCH` creates new version, original `isLatest=false` | |
| Bulk / agentic forget | ⚠️ `delete_all` by scope, `batch_delete` 1000/call | ✅ `forget-matching` by NL query or ids, `dryRun`, `maxForget` | SM's is LLM-driven with server-owned identity handles |
| Batch operations | ✅ Platform `batch_update`/`batch_delete` 1000/call; OSS none | ✅ bulk add, bulk delete by ids or container tags | |
| User profiles | ✅ Platform-only, JSON-Schema-shaped, generation jobs | ✅ core feature — static + dynamic + **buckets** | SM's buckets are a third topical axis; org-level + per-space |
| Custom instructions | ✅ `custom_instructions` (both products) | ✅ `entityContext` (per add / per container, ≤1500 chars) + filterPrompt | |
| Custom categories | ✅ Platform-only (per project or per add) | ⚠️ via profile **buckets** (topical) | Different mechanisms |
| Filtered writes | ❌ | ✅ `filterByMetadata` scopes which existing memories seed extraction | |
| Multi-tenancy model | `user_id`/`agent_id`/`run_id` (**`app_id` Platform-only**) | **`containerTag`** — hashed dedicated vector namespace | SM: auto-creates "space"; name ≤100 chars, `^[a-zA-Z0-9_:-]+$` |
| Tenant-scoped credentials | ⚠️ org/project API keys | ✅ container-scoped keys (read/write per tag), 403 enforcement at data layer | SM's scoped keys are a stronger primitive |
| Org / project hierarchy | ✅ Platform (orgs, projects, member roles) | ✅ orgs + spaces + members; tag merge | OSS mem0 has no org/project concept |
| Self-host vs cloud | ✅ both (OSS library, OSS Docker server, Platform) | ✅ both (single binary, or managed cloud) | Both: self-host lacks connectors/MCP/enterprise features |
| Fully offline / local-LLM mode | ✅ (Ollama, LM Studio, vLLM, llama.cpp, FastEmbed) | ✅ (any OpenAI-compatible endpoint; local embeddings default) | SM default embedding `Xenova/bge-base-en-v1.5` 768d, no API key |
| Vector store choice | ✅ ~25+ (Qdrant default, pgvector, Pinecone, Chroma, Milvus, Redis…) | ❌ not user-configurable | mem0 far more flexible at storage layer |
| LLM/embedder choice | ✅ 18 LLM providers, 11 embedders (OSS) | ⚠️ bring your own for self-host; model fixed on cloud | |
| MCP server | ✅ `https://mcp.mem0.ai` (Platform key) | ✅ MCP with OAuth, team spaces | SM MCP cloud-only |
| Agent self-signup | ✅ `mem0 init --agent` (5/IP/day, claimable) | ❌ | mem0-unique; notable for agent-first onboarding |
| Coding-agent plugins | ✅ Claude Code (v0.3.1), Cursor, Codex, Kimi, OpenCode, Antigravity, Pi, OpenClaw, Hermes | ✅ Claude Code, Codex, OpenCode, Cursor, OpenClaw, Hermes, Muse Code, Grok Bot | Comparable breadth |
| SDKs | ✅ Python 2.x, TS 3.x, 2 CLIs | ✅ Python, JS/TS | mem0 ships a CLI; SM relies on REST + SDKs |
| Webhooks | ✅ Platform-only, project-scoped | ⚠️ not documented | |
| Analytics / observability | ⚠️ dashboard, event feed, Copilot | ✅ dedicated Analytics & Monitoring (usage, errors, logs) | |
| Feedback loop on retrieval | ✅ Platform `feedback(memory_id)` — POSITIVE/NEGATIVE/VERY_NEGATIVE | ⚠️ via memory review approve/decline | Different targets: retrieval quality vs inference validity |
| Summaries | ✅ Platform `get_summary(filters)` | ✅ profile static/dynamic + `include.summaries` | |
| Export | ✅ Platform schema-driven export jobs | ✅ data exportable (self-host, reset API) | |
| Encryption | ⚠️ not documented on pages fetched | ✅ TLS in transit, AES-256 class at rest | |
| Compliance | ⚠️ not documented on pages fetched | ✅ SOC 2 Type II, GDPR, HIPAA BAA (Scale/Ent) | SM documents this explicitly |
| No-training guarantee | ⚠️ not documented on pages fetched | ✅ "never used to train models — every plan, free or paid" | |
| Published benchmark numbers | ✅ LoCoMo 92.5, LME 94.4, BEAM 64.1/48.6 | ❌ qualitative claims only, links to `/research` | mem0 numbers are Platform-only, ±1pt CI |
| Open benchmark harness | ✅ `mem0ai/memory-benchmarks` | ✅ `supermemoryai/memorybench` (MIT) | MemoryBench runs Mem0 + Zep as providers |
| Third-party comparison tooling | ❌ | ✅ MemoryBench + Claude Code skill | |
| Open-source license | ✅ Apache-2.0 (repo `github.com/mem0ai/mem0`) | ✅ open source (`git.new/memory`, SMFS also OSS) | |

---

## Key takeaways for Foresight

1. **The industry moved away from UPDATE/DELETE reconciliation toward ADD-only + ranking.** mem0's v3 deleted its two-call extract-then-diff pipeline, removed UPDATE/DELETE events entirely, and replaced consolidation with a *search-time* mechanism (Decay) plus an *async background* one (Dream). This is a direct architectural signal: reconciliation at write time was expensive and lossy; keep everything, rank by recency/entity/temporal fit, and consolidate in the background.
2. **Graph memory split into two incompatible philosophies.** mem0: untyped co-occurrence that only touches ranking, with no queryable graph and no `relations` payload. Supermemory: three typed edge semantics (`updates`/`extends`/`derives`) that change what counts as current truth and are returned inline to the caller. For a Postgres-backed system, Supermemory's typed edges are the more useful model — they're expressible as a plain relational table with an `edge_type` enum, whereas mem0's ranking-only graph reduces to an entity-overlap boost.
3. **Uncertainty is the unsolved problem.** mem0's ADD-only design scores 93.6 on knowledge update and **32.5 on contradiction resolution** at 10M tokens; Supermemory's answer is to flag `derives` as `isInference` and **down-weight them until a human approves**. That review-queue pattern is cheap to implement on Postgres and is a genuine differentiator neither product does well automatically.
4. **Token cost is the metric that separates benchmark from production.** mem0 publishes ~6,700–7,000 tokens/query and argues full-context baselines burn 25,000+. Any Foresight benchmark should report accuracy *and* mean context tokens at a fixed retrieval budget (`top_k`), with a declared judge and judge-variance.
5. **Small-benchmark saturation is real.** mem0's BEAM 1M→10M collapse (64.1 → 48.6, temporal 61.8 → 16.3) is the strongest public evidence that LoCoMo/LongMemEval scores overstate production capability. Foresight should evaluate at a scale where the answer isn't brute-forceable.
6. **Self-host parity is the recurring gap in both products** — mem0's best features (graph, decay, temporal, Dream, webhooks, custom categories) are Platform-only, and mem0 states its benchmark numbers come from the managed platform; Supermemory's connectors and MCP are cloud-only and its benchmarks aren't published. A fully self-hostable system that publishes reproducible numbers is an open position.
