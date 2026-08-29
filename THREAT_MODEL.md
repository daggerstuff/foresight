# Foresight Threat Model & Security Architecture

> Comprehensive security architecture, trust boundaries, STRIDE threat analysis, and risk mitigations for the Foresight persistent agent memory ecosystem.

---

## 1. System Overview & Architecture

Foresight is an enterprise-grade persistent memory subsystem for AI agents operating across diverse developer interfaces (IDE plugins, FastMCP servers, CLI, and ambient OS hooks).

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Agent Interfaces & Clients                         │
│  [MastraCode Plugin]  [Claude Code Hook]  [Cursor MCP]  [Foresight CLI/TUI] │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ (FastMCP / Unix Socket / HTTP / TLS)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Foresight Core MCP Server Engine                      │
│  ┌─────────────────────────┐  ┌──────────────────────────────────────────┐  │
│  │   Tenant Auth Gating    │  │       Context Budget & Rate Limiter      │  │
│  └────────────┬────────────┘  └────────────────────┬─────────────────────┘  │
│               ▼                                    ▼                        │
│  ┌─────────────────────────┐  ┌──────────────────────────────────────────┐  │
│  │   Hybrid Search Engine  │  │        AES-256-GCM Encryption Layer      │  │
│  │   (pgvector + BM25/RRF) │  │        (Key Derivation & Rotation)       │  │
│  └────────────┬────────────┘  └────────────────────┬─────────────────────┘  │
└───────────────┼────────────────────────────────────┼────────────────────────┘
                ▼                                    ▼
┌───────────────────────────────┐    ┌────────────────────────────────────────┐
│     PostgreSQL 17 (Neon)      │    │             Redis Cluster              │
│  • Memory Vectors & Metadata  │    │  • Ephemeral Cache & PubSub            │
│  • Context Blocks & Traces    │    │  • Distributed Locks & Rate Limits     │
└───────────────────────────────┘    └────────────────────────────────────────┘
```

---

## 2. Assets & Data Classification

| Asset | Classification | Description | Storage Location | Protection Controls |
| :--- | :--- | :--- | :--- | :--- |
| **Agent Memories** | Confidential / Clinical PHI | Distilled facts, decisions, and user guidance | PostgreSQL (`memories` table) | AES-256-GCM row-level encryption, Tenant isolation |
| **Context Blocks** | Confidential | Standing directives, preferences, pending tasks | PostgreSQL (`context_blocks` table) | Versioned history, tenant-scoped access |
| **Vector Embeddings** | Confidential | High-dimensional semantic vectors | PostgreSQL (`pgvector` indexes) | Tenant-segregated cosine similarity queries |
| **Master Encryption Keys** | Secret | Root 256-bit symmetric keys (`FORESIGHT_ENCRYPTION_KEY`) | Environment / Secret Manager | In-memory key expansion only, zero persistence |
| **Tenant Access Tokens** | Secret | Tenant authentication tokens | HTTP Headers / CLI config | Constant-time comparison, SHA-256 hashing |
| **System Telemetry & Logs** | Internal | Operational metrics, error traces, audit events | Redis / Structured Logs | PHI redaction filter, PII stripping |

---

## 3. Trust Boundaries & Threat Actors

### Trust Boundaries

1. **TB-1: Agent / Client to FastMCP Server** — Boundary between untrusted/semi-trusted local AI agents and the Foresight server process.
2. **TB-2: FastMCP Server to Neon PostgreSQL** — Boundary traversing TLS network link to remote managed Postgres cluster.
3. **TB-3: Server to Redis Companion** — Boundary between local/network caching and the memory state machine.
4. **TB-4: Multi-Tenant Boundary** — Logical boundary between distinct `tenant_id` / `account_id` scopes within shared database tables.
5. **TB-5: Outbound LLM Provider API** — Network boundary between Foresight and external model providers (OpenAI, Anthropic, Ollama).

### Threat Actors

- **TA-1: Malicious Prompt / Injection Vector** — Indirect prompt injection embedded within context or transcripts attempting memory pollution.
- **TA-2: Rogue Subagent / Compromised Client** — Process attempting to query cross-tenant memories or escalate privileges.
- **TA-3: Network Adversary / MitM** — Eavesdropper attempting to intercept unencrypted memory sync or telemetry streams.
- **TA-4: Co-Tenant in Shared Environment** — Malicious tenant attempting to craft SQL or filter injection to access neighboring tenant data.

---

## 4. STRIDE Threat Analysis & Mitigations

### 1. Spoofing (Authenticity)
- **Threat**: Attacker presents spoofed `tenant_id` or forged authorization header to access other tenant records.
- **Mitigation**:
  - Mandatory token verification via `TenantMiddleware`.
  - ContextVar thread/task-local storage for authenticated tenant context (`get_current_tenant_id()`).
  - Row-Level Security (RLS) enforcement on all database queries with explicit parameterized tenant filtering.

### 2. Tampering (Integrity)
- **Threat**: Malicious memory payload modifies memory relationships, corrupts decay curves, or injects SQL.
- **Mitigation**:
  - 100% parameterized SQL queries via `psycopg3` (zero string-concatenated SQL expressions).
  - Strict Pydantic and dataclass input schema validation before entering storage pipelines.
  - Cryptographic integrity verification via AES-256-GCM authentication tags (GHASH).

### 3. Repudiation (Non-Repudiation)
- **Threat**: Agent or user denies creating, updating, or deleting sensitive clinical or project decisions.
- **Mitigation**:
  - Full temporal audit log (`memory_versions` and `audit_events` tables).
  - Immutable historical version tracking on every memory update and archival event.
  - Structured audit trail recording timestamp, actor, mutation delta, and tenant ID.

### 4. Information Disclosure (Confidentiality)
- **Threat**: Memory data or encryption keys leaked through logs, memory dumps, or cross-tenant query contamination.
- **Mitigation**:
  - AES-256-GCM envelope encryption for sensitive memory content and attributes.
  - Zero logging of raw credentials, database connection strings, or decrypted clinical payloads.
  - Automatic memory redaction filter stripping email addresses, JWTs, and API keys.

### 5. Denial of Service (Availability)
- **Threat**: Agent floods server with oversized context payloads, high-cardinality vector queries, or unpaginated fetches.
- **Mitigation**:
  - Token budget enforcement and context truncation limiters in `ContextEngine`.
  - Connection pooling with bounded pool size and connection timeouts via `psycopg_pool`.
  - Circuit breakers (`CircuitBreaker` pattern) protecting external LLM and database dependencies.
  - Hard pagination limits and bounded result sets (`max_results=50`, `max_pages=100`).

### 6. Elevation of Privilege (Authorization)
- **Threat**: Standard user invokes administrative tools (e.g. `rotate_master_key`, `purge_all_tenants`).
- **Mitigation**:
  - Role-based tool access separation within FastMCP tool registration.
  - Administrative tools require superuser tenant credentials and dedicated secondary validation.

---

## 5. Cryptographic Implementation Details

- **Algorithm**: AES-256-GCM (Authenticated Encryption with Associated Data).
- **Key Derivation**: HKDF-SHA256 (HMAC-based Extract-and-Expand Key Derivation).
- **IV Generation**: 96-bit cryptographically secure random nonce (`os.urandom(12)` or `secrets.token_bytes(12)`).
- **Tag Validation**: 128-bit authentication tag checked prior to payload release; tampering immediately aborts decryption.
- **Key Rotation**: Supported live via `manage_encryption(action="rotate_key")` with versioned key identifiers.

---

## 6. Security Assurance & Continuous Verification

1. **Automated SAST & Security Scans**:
   - `bandit` scanning enforced in pre-commit and CI pipeline.
   - `ruff` security rules (`S`, `B`, `A`) active across codebase.
2. **Automated Secret Detection**:
   - Secret scanning hooks preventing commit of API keys, private keys, or credentials.
3. **Multi-Tenant Isolation Tests**:
   - Automated regression test suite (`tests/test_tenant_isolation.py`, `tests/test_encryption.py`, `tests/test_auth.py`) executed on every build.
