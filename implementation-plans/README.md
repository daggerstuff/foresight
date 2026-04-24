# Audit Fix Implementation Plans

This directory contains implementation plans for fixes identified in the ML/RAG systems audit.

## Priority Order

### P0 (Critical - Fix Immediately)
1. **SQL Injection** - `sql-injection-fix.md` (foresight-mcp-m62)
2. **Connection Pool** - `connection-pool-enforcement.md` (foresight-mcp-iej)
3. **Race Condition** - `race-condition-fix.md` (foresight-mcp-9h5)

### P1 (High - Fix This Week)
4. **Ghost Memory Cleanup** - `ghost-memory-cleanup.md` (foresight-mcp-1sm)
5. **Circuit Breaker** - `circuit-breaker-pattern.md` (foresight-mcp-s41)

### P2 (Medium - Fix This Month)
6. **N+1 Query Pattern** - `n-plus-one-fix.md` (foresight-mcp-2x4)
7. **Embedding Validation** - `embedding-dimension-validation.md` (foresight-mcp-4hw)
8. **TF-IDF Labeling** - `tfidf-labeling-fix.md` (foresight-mcp-7xr)
9. **RRF Weight Tuning** - `rrf-weight-tuning.md` (foresight-mcp-nul)
10. **Graph Edge Decay** - `graph-edge-decay.md` (foresight-mcp-auw)

## Dependencies

``
foresight-mcp-s41 (Circuit Breaker)
    ↓
foresight-mcp-iej (Connection Pool)

foresight-mcp-m62 (SQL Injection) - No dependencies
foresight-mcp-9h5 (Race Condition) - No dependencies
``

## Quick Start

```bash
# See all issues
bd ready

# Show issue details
bd show foresight-mcp-m62

# Claim work
bd update foresight-mcp-m62 --claim

# Close when done
bd close foresight-mcp-m62
```

---
Audit Date: 2026-04-24
Audit Report: `../audit-ml-rag-systems.md`
