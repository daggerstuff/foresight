---
sidebar_label: Overview
title: API Reference Overview
---

# API Reference

Complete API documentation for Foresight memory, context blocks, and curation
workflows.

## Available APIs

| API                                | Description                      |
| ---------------------------------- | -------------------------------- |
| [Python API](./python-api)         | Python helper and tool reference |
| [TypeScript API](./typescript-api) | TypeScript SDK reference         |
| [CLI Reference](./cli-reference)   | Command-line interface           |

## Quick links

### Memory operations

- `store_memory(content, ...)` - Store a new memory
- `query_memories(query, ...)` - Search memories
- `list_memories(limit=10, offset=0, ...)` - List memories
- `get_memory(id, ...)` - Retrieve a specific memory
- `update_memory(id, ...)` - Update memory content or metadata
- `delete_memory(id, ...)` - Delete a memory
- `memory_status(...)` - Get system status

### Context block operations

- `list_context_blocks(user_id, tenant_id="default")` - List non-empty context
  blocks
- `get_context_block(label, user_id, tenant_id="default")` - Read a block
- `update_context_block(label, content, user_id, tenant_id="default")` - Replace
  a block
- `add_context_guidance(line, user_id, tenant_id="default")` - Append a guidance
  line
- `reset_context_block(label, user_id, tenant_id="default")` - Reset a block to
  its default
- `clear_context_block(label, user_id, tenant_id="default")` - Clear a block
- `get_context_whisper(user_id, tenant_id="default")` - Get whisper-ready
  guidance XML
- `get_context_snapshot(user_id, tenant_id="default")` - Get the full XML
  snapshot
- `manage_context_blocks(ContextBlockAction(...), user_id=...)` - MCP-style
  block management

### Curation workflow

- `manage_curation_runs(CurationRunAction(...), user_id=...)` - Create, inspect,
  cancel, and archive curation runs
- `CurationRunAction(action="create", source_bank_id=..., ...)` - Define async
  curation jobs

### Hooks and real-time updates

- `register_hook(name, event_type, url, **options)` - Register a hook
- `list_hooks()` - List hooks
- `unregister_hook(hook_id)` - Remove a hook
- `ws_subscribe(subscription_id, event_types, **options)` - Subscribe to server
  events

## Migration note

Older `subconscious` names remain available as compatibility aliases, but public
documentation now uses Foresight-native `context block` and `curation`
terminology.
