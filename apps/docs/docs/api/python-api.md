---
sidebar_label: Python API
title: Python API Reference
---

# Python API Reference

Complete Python API documentation for Foresight memory, context blocks, and
curation workflows.

## Memory operations

### store_memory

```python
def store_memory(params: StoreMemoryParams) -> str
```

Stores a new memory record. `StoreMemoryParams` carries `content` plus the
same store options `manage_memories` accepts (`category`, `scope`, `retention`,
`importance`, `emotional_context`, `metrics`, `relation_type`,
`related_memory_id`, `tags`, `expires_at`, `memory_type`, `auto_tag`,
`user_id`).

```python
from foresight import StoreMemoryParams, store_memory

store_memory(StoreMemoryParams(content="Prefers concise replies", category="preference"))
```

### query_memories

```python
def query_memories(params: QueryMemoriesParams) -> str
```

Searches memories by text and ranking signals. `QueryMemoriesParams` carries
`query`, `limit`, `use_hybrid`, `min_importance`, `offset`, and `user_id`.

### list_memories

```python
def list_memories(
    limit: int = 10,
    offset: int = 0,
    user_id: str | None = None,
) -> str
```

Lists memories for a user.

### get_memory

```python
def get_memory(
    memory_id: str,
    user_id: str | None = None,
    min_importance: float = 0.1,
) -> str
```

Retrieves one memory by ID.

### update_memory

```python
def update_memory(params: UpdateMemoryParams) -> str
```

Updates memory content or metadata. `UpdateMemoryParams` carries `memory_id`
plus `content`, `category`, `scope`, `retention`, `tags`, `relation_type`,
`related_memory_id`, and `user_id`.

### delete_memory

```python
def delete_memory(memory_id: str, user_id: str | None = None) -> str
```

Deletes a memory.

### memory_status

```python
def memory_status(
    user_id: str | None = None,
    include_trends: bool = False,
    timeframe: str = "30 days",
) -> str
```

Returns system status and optional trend data.

## Context block helpers

### list_context_blocks

```python
def list_context_blocks(user_id: str, tenant_id: str = "default") -> list[dict]
```

Lists non-empty persisted context blocks for a `(user_id, tenant_id)` pair.

### get_context_block

```python
def get_context_block(
    label: str,
    user_id: str,
    tenant_id: str = "default",
) -> str | None
```

Reads one context block.

### update_context_block

```python
def update_context_block(
    label: str,
    content: str,
    user_id: str,
    tenant_id: str = "default",
) -> None
```

Replaces a block's content.

### add_context_guidance

```python
def add_context_guidance(
    line: str,
    user_id: str,
    tenant_id: str = "default",
) -> None
```

Appends a line to the `guidance` block.

### reset_context_block

```python
def reset_context_block(
    label: str,
    user_id: str,
    tenant_id: str = "default",
) -> None
```

Restores a block's default content.

### clear_context_block

```python
def clear_context_block(
    label: str,
    user_id: str,
    tenant_id: str = "default",
) -> None
```

Clears a block.

### get_context_whisper

```python
def get_context_whisper(
    user_id: str,
    tenant_id: str = "default",
) -> str
```

Returns the whisper-ready XML payload.

### get_context_snapshot

```python
def get_context_snapshot(
    user_id: str,
    tenant_id: str = "default",
) -> str
```

Returns the full XML snapshot of non-empty blocks.

## MCP-style actions

### ContextBlockAction

```python
class ContextBlockAction(BaseModel):
    action: Literal["list", "get", "update", "reset", "clear"]
    label: str | None = None
    content: str | None = None
```

### manage_context_blocks

```python
def manage_context_blocks(params: ManageContextBlocksParams) -> str
```

Manages context blocks through the same action-oriented contract exposed by the
MCP server. `ManageContextBlocksParams` carries `action`, `label` (with the
`block_name` / `block_type` / `name` / `block` / `block_id` / `key` aliases),
`content`, `agent_id`, `project`, `visibility`, and `user_id`.

Returns a JSON envelope string such as:

```json
{
  "ok": true,
  "action": "list",
  "blocks": [{ "label": "project_context", "content": "..." }]
}
```

### CurationRunAction

```python
class CurationRunAction(BaseModel):
    action: Literal["create", "get", "list", "cancel", "archive"]
    run_id: str | None = None
    source_bank_id: str | None = None
    output_bank_id: str | None = None
    policy_mode: Literal["preserve", "rebalance", "rebuild"] = "rebalance"
    tool_access: Literal["disabled", "observe", "operate"] = "observe"
    output_mode: Literal["reviewable_output", "in_place"] = "reviewable_output"
    instructions: str | None = None
    transcript_bundle: list[dict[str, Any]] | None = None
    session_id: str | None = None
    project_path: str | None = None
    limit: int = 20
```

### manage_curation_runs

```python
def manage_curation_runs(params: ManageCurationRunsParams) -> str
```

Creates and manages asynchronous curation runs. `ManageCurationRunsParams`
carries `action`, `run_id`, `source_bank_id`, `output_bank_id`, `policy_mode`,
`tool_access`, `output_mode`, `instructions`, `run_clustering`,
`transcript_bundle`, `session_id`, `project_path`, and `user_id`.

### Behavior notes

- `create` defaults to a separate reviewable output bank
- `output_mode="in_place"` requires `tool_access="operate"`
- `output_mode="in_place"` always uses an auto-generated staging bank and
  rejects `output_bank_id` overrides
- transcript bundles require `tool_access="operate"`
- `in_place` runs stage into an auto-generated bank, archive source rows on
  success, and then promote staged rows into the source bank
- `failed` and `canceled` runs leave any already-written staged output untouched
  for inspection
- `archive` only works after a run reaches a terminal state

Tool responses are JSON envelope strings:

```json
{
  "ok": true,
  "action": "get",
  "run": {
    "id": "cur_abc123def456",
    "status": "completed"
  }
}
```

## Migration: parameter objects

Every MCP tool and public Python entry point on this page now takes exactly one
parameter object instead of a long keyword list. Field names, defaults,
validation, error messages, and return values are unchanged — only the calling
convention moved.

The MCP wire schema changes with it. `fastmcp` **nests** a single model
parameter under the parameter name rather than flattening its fields, so the
advertised `inputSchema` for each affected tool is
`{"type": "object", "properties": {"params": {...}}, "required": ["params"]}`.
Arguments go inside `params`:

```jsonc
// before
{"name": "manage_memories", "arguments": {"action": "store", "content": "..."}}

// after
{"name": "manage_memories", "arguments": {"params": {"action": "store", "content": "..."}}}
```

### `manage_memories`

```python
# before
manage_memories(action="store", content="Prefers concise replies", category="preference")

# after
manage_memories(ManageMemoriesParams(action="store", content="Prefers concise replies", category="preference"))
```

The nested `options` and `updates` payloads are unchanged and still accepted
alongside the flat fields:

```python
manage_memories(ManageMemoriesParams(options=MemoryAction(action="delete", memory_id=mid)))
```

### `search_memories`

```python
# before
search_memories(query="gym routine", limit=5, agent_id="agent-a")

# after
search_memories(SearchMemoriesParams(query="gym routine", limit=5, agent_id="agent-a"))
```

### `inject_context`

```python
# before
inject_context(conversation_text=message, max_memories=5)

# after
inject_context(InjectContextParams(conversation_text=message, max_memories=5))
```

Over MCP the same call is `{"params": {"conversation_text": "...", "max_memories": 5}}`.

### Parameter object per entry point

| Entry point                    | Parameter object                |
| ------------------------------ | ------------------------------- |
| `manage_memories`              | `ManageMemoriesParams`          |
| `search_memories`              | `SearchMemoriesParams`          |
| `manage_context_blocks`        | `ManageContextBlocksParams`     |
| `manage_curation_runs`         | `ManageCurationRunsParams`      |
| `inject_context`               | `InjectContextParams`           |
| `query_memories_temporal`      | `QueryMemoriesTemporalParams`   |
| `synthesize_profile`           | `SynthesizeProfileParams`       |
| `manage_memory_versions`       | `ManageMemoryVersionsParams`    |
| `reflect`                      | `ReflectParams`                 |
| `manage_mental_models`         | `ManageMentalModelsParams`      |
| `process_session_transcript`   | `ProcessSessionTranscriptParams`|
| `capture_in_flight_memory`     | `CaptureInFlightMemoryParams`   |
| `get_system_status`            | `GetSystemStatusParams`         |
| `ingest_document_file`         | `IngestDocumentFileParams`      |
| `link_memories`                | `LinkMemoriesParams`            |
| `manage_inferred`              | `ManageInferredParams`          |
| `analyze_memories`             | `AnalyzeMemoriesParams`         |
| `store_memory`                 | `StoreMemoryParams`             |
| `get_relevant_memories`        | `GetRelevantMemoriesParams`     |
| `update_memory`                | `UpdateMemoryParams`            |
| `set_decay_config`             | `SetDecayConfigParams`          |
| `create_document`              | `CreateDocumentParams`          |
| `query_memories`               | `QueryMemoriesParams`           |

All twenty-three objects are re-exported from the `foresight` package.

## Migration note

Legacy `subconscious` helper names remain available as compatibility aliases,
but new code should use the Foresight-native context block helpers above.

## Related

- [CLI Reference](./cli-reference)
- [TypeScript API](./typescript-api)
