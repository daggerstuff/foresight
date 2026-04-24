# Foresight MCP - ML/RAG Systems Audit Report

**Audit Date:** 2026-04-24
**Scope:** Full-coverage ML/RAG systems audit covering RAG pipeline, vector search, temporal graph, memory scoring, and observability

---

## Executive Summary

The Foresight MCP system implements a hybrid retrieval architecture combining keyword, semantic (TF-IDF), graph, and temporal signals. **Critical finding:** The system lacks a true embedding service despite having `vector_id` fields throughout - semantic search uses pure TF-IDF cosine similarity without neural embeddings. The architecture shows strong multi-tenancy patterns but has significant gaps in production readiness for ML workloads.

---

## 1. RAG Pipeline Audit

### CRITICAL Issues

| # | Issue | Location | Severity |
|---|-------|----------|----------|
| C1 | **No embedding service implementation** - `vector_id` column exists but no embedding generation code found | `server.py:140`, entire codebase | CRITICAL |
| C2 | **No vector index management** - no similarity thresholds, no dimension validation | Throughout | CRITICAL |
| C3 | **TF-IDF semantic search is not true semantic search** - uses bag-of-words cosine, not sentence embeddings | `hybrid_retriever.py:320-420` | CRITICAL |

**File: `hybrid_retriever.py:320-420`**
```python
def _semantic_search(self, conn, query, user_id, tenant_id, limit):
    """Semantic search using TF-IDF cosine similarity."""
    # Builds TF-IDF vectors from tokenized words - NO neural embeddings
    # Pure Python implementation -- no external ML dependencies
```

### HIGH Issues

| # | Issue | Location | Impact |
|---|-------|----------|--------|
| H1 | No embedding cache for query/results | `hybrid_retriever.py` | Re-computing TF-IDF on every query |
| H2 | No query preprocessing/normalization | `hybrid_retriever.py:273` | "Anxiety" vs "anxious" treated as different |
| H3 | Context window usage not optimized - retrieves `limit*3` candidates then RRF | `hybrid_retriever.py:175-196` | May miss relevant results in long tail |
| H4 | No document chunking strategy for large memories | `server.py:memories table` | Full content in single vector_id |

### MEDIUM Issues

| # | Issue | Recommendation |
|---|-------|----------------|
| M1 | No query expansion (synonyms, stemming) | Add query rewriting layer |
| M2 | No re-ranking after initial retrieval | Add cross-encoder re-ranker |
| M3 | RRF k=60 constant never tuned | Grid search for optimal k |

**File: `hybrid_retriever.py:108`**
```python
RRF_K = 60  # Standard RRF constant - NEVER TUNED
```

---

## 2. Vector Search Audit

### CRITICAL Issues

| # | Issue | Evidence |
|---|-------|----------|
| C1 | **No vector database** - `vector_id TEXT` column suggests external vectors but no integration found | `server.py:140` |
| C2 | **No similarity threshold** for retrieval filtering | `hybrid_retriever.py` - no `min_similarity` parameter |
| C3 | **No dimensionality validation** - would accept any vector dimension | No embedding dimension constants found |

### HIGH Issues

| # | Issue | Location |
|---|-------|----------|
| H1 | Hybrid retrieval fusion weights are hardcoded | `hybrid_retriever.py:113-118` |
| H2 | No A/B testing framework for weight tuning | No experiment tracking |
| H3 | Graph-entity search uses LIKE matching (no semantic entity resolution) | `graph_store.py:447-475` |

**File: `hybrid_retriever.py:113-118`**
```python
DEFAULT_WEIGHTS = {
    "keyword": 1.0,   # Never tuned
    "semantic": 0.7,  # Misleading - this is TF-IDF, not semantic
    "graph": 0.8,
    "temporal": 0.6,
}
```

### MEDIUM Issues

| # | Issue | Fix |
|---|-------|-----|
| M1 | No index health monitoring | Add index size/doc count tracking |
| M2 | Ghost memories excluded from semantic search but not from keyword | `hybrid_retriever.py:347` vs `273` |
| M3 | Entity relationships don't decay over time | Graph becomes stale |

---

## 3. Embedding Service Audit

### CRITICAL Issues

| # | Issue |
|---|-------|
| C1 | **NO EMBEDDING SERVICE EXISTS** - Despite `vector_id` fields throughout, no embedding generation code found in codebase |
| C2 | No embedding model configuration |
| C3 | No batch inference pipeline |
| C4 | No embedding caching layer |

**Recommendation:** Implement embedding service with:
- Model registry (text-embedding-ada-002, bge-large, etc.)
- Batch inference queue
- Embedding cache with TTL
- Dimension validation per model

---

## 4. Temporal Graph Audit

### HIGH Issues

| # | Issue | Location |
|---|-------|----------|
| H1 | Exponential decay uses fixed half-life (168h/week) - no category customization by default | `temporal_service.py:26` |
| H2 | Time decay only recalculated on access or batch job | `temporal_service.py:253-315` |
| H3 | Graph edge weights don't incorporate temporal decay | `graph_store.py` |
| H4 | Freshness trend calculation is threshold-based, not learned | `temporal_service.py:129-158` |

**File: `temporal_service.py:26`**
```python
@dataclass
class DecayConfig:
    half_life_hours: float = 168.0  # 1 week - default for ALL categories
    # No per-memory-type customization
```

### MEDIUM Issues

| # | Issue | Impact |
|---|-------|--------|
| M1 | No visualization of temporal decay curves | Hard to debug importance |
| M2 | Batch decay updates could lock table on large datasets | `temporal_service.py:269-307` |
| M3 | `hours_elapsed` calculation uses `datetime.now()` - inconsistent across distributed runs | `temporal_service.py:109` |

---

## 5. Memory Scoring Audit

### HIGH Issues

| # | Issue | Location |
|---|-------|----------|
| H1 | Importance calculation mixes time decay AND activation boost AND trend modifier | `temporal_service.py:532` |
| H2 | No validation that importance stays in [0,1] bounds | `temporal_service.py:532: `min(1.0, importance * time_score * activation_boost * trend_mod)` |
| H3 | `min_importance=0.1` default is arbitrary | `temporal_service.py:483` |
| H4 | Trend categories (stable/strengthening/weakening/stale) not validated against ground truth | `temporal_service.py:129-158` |

### MEDIUM Issues

| # | Issue |
|---|-------|
| M1 | No calibration of importance scores against actual relevance |
| M2 | Activation counter unbounded - could dominate other signals |
| M3 | Synthesis compression ratio never used for anything | `memory_components.py:189` |

---

## 6. LLM Integration Audit

### HIGH Issues

| # | Issue | Location |
|---|-------|----------|
| H1 | SocraticGate uses hardcoded thresholds | `memory_components.py:85-138` |
| H2 | No prompt templates stored/managed | Throughout |
| H3 | Entity extraction types hardcoded | `graph_store.py:85-86` |
| H4 | No token counting or context window management | `subconscious.py` |

**File: `memory_components.py:85-138`**
```python
class SocraticGate:
    def evaluate(self, memory, user_id):
        # Hardcoded logic - no LLM prompting
        # Would be easy to LLM-ify but currently rule-based
```

---

## 7. Data Integrity Audit

### CRITICAL Issues

| # | Issue | Impact |
|---|-------|--------|
| C1 | **No schema validation on memory ingestion** - `content TEXT` accepts anything | `server.py:126-145` |
| C2 | **No referential integrity checks** in neo4j-style graph (SQLite-backed) | `graph_store.py` |
| C3 | **JSON columns parsed without validation** - `json.loads()` on `metrics`, `emotional_context` | `server.py:953-956` |

### HIGH Issues

| # | Issue | Location |
|---|-------|----------|
| H1 | Foreign key from `memory_entity_links` to `memories` uses `ON DELETE CASCADE` but no validation of orphaned entities | `server.py:156` |
| H2 | `category` column defaults to `'general'` but schema says `'fact'` | `temporal_service.py:600` vs `server.py:126` |
| H3 | Tenant isolation relies solely on query parameters - no row-level security | All queries |

### MEDIUM Issues

| # | Issue |
|---|-------|
| M1 | No migration rollback scripts |
| M2 | `schema_migrations` table has no checksum validation |
| M3 | Soft-delete not implemented - only hard DELETE |

---

## 8. Observability Audit

### CRITICAL Issues

| # | Issue |
|---|-------|
| C1 | **No metrics export** - no Prometheus/OpenTelemetry integration |
| C2 | **No latency tracking** on retrieval queries |
| C3 | **No distributed tracing** across hybrid retrieval signals |

### HIGH Issues

| # | Issue | Location |
|---|-------|----------|
| H1 | Logging is basic `logger.info()` - no structured logging | `hybrid_retriever.py:28` |
| H2 | No retrieval quality metrics (NDCG, MAP, recall@k) | Nowhere |
| H3 | No error rate tracking | Nowhere |
| H4 | No slow query logging | `hybrid_retriever.py` - no query timing |

### MEDIUM Issues

| # | Issue |
|---|-------|
| M1 | No alerting on anomalous retrieval patterns |
| M2 | No dashboard for memory health (distribution of trends, categories) |
| M3 | No user-facing retrieval quality feedback loop |

---

## 9. Test Coverage Gaps

### Existing Coverage (Good)
- `test_hybrid_retriever.py` - Comprehensive RRF and signal tests
- `test_temporal.py` - Temporal decay tests

### Missing Coverage (CRITICAL)

| Area | Missing Tests |
|------|---------------|
| Embedding | No embedding service = no tests |
| Graph traversal depth limits | No test for max_depth validation |
| Tenant isolation failures | No multi-tenant leak tests |
| Concurrency | No parallel query tests |
| Recovery | No disaster recovery tests |
| Performance | No load tests, latency SLO tests |

---

## Prioritized Fix List

### Critical (Fix Immediately)
1. **C1**: Implement actual embedding service or remove `vector_id` deception
2. **C2**: Add data validation layer for memory ingestion
3. **C3**: Implement similarity thresholds for retrieval quality

### High Priority
1. **H1**: Add query preprocessing pipeline
2. **H2**: Implement retrieval metrics (latency, recall)
3. **H3**: Tune RRF weights with labeled data
4. **H4**: Add observability dashboard

### Medium Priority
1. **M1**: Query expansion with synonyms
2. **M2**: Re-ranking with cross-encoder
3. **M3**: Embedding cache with TTL
4. **M4**: Graph edge decay over time

---

## Architecture Verification Checklist

- [ ] Embedding model selected and deployed
- [ ] Vector database (pgvector, Qdrant, Weaviate) provisioned
- [ ] Embedding dimension documented and validated
- [ ] Retrieval latency SLO defined (<100ms p99)
- [ ] Retrieval quality metrics defined (recall@10 > 0.7)
- [ ] A/B testing framework for weight tuning
- [ ] Structured logging (JSON) implemented
- [ ] Prometheus metrics exported
- [ ] Alerting on retrieval failures
- [ ] Data validation schema defined

---

## Conclusion

The Foresight MCP system has a solid foundation for hybrid retrieval but critically lacks:
1. **Actual embedding infrastructure** (uses TF-IDF cosine, not neural embeddings)
2. **Production observability** (no metrics, tracing, or proper logging)
3. **Data validation** (JSON parsing without schema validation)

The "hybrid" retrieval is functional but would benefit significantly from true semantic embeddings and proper ML infrastructure. The temporal decay logic is sound but needs per-category tuning and better integration with graph edges.

**Recommendation:** Prioritize embedding service implementation and observability infrastructure before scaling to production workloads.
