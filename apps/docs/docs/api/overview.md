---
sidebar_label: Overview
title: API Reference Overview
---

# API Reference

Complete API documentation for Foresight Memory Architecture.

## Available APIs

| API | Description |
|-----|-------------|
| [Python API](./python-api) | Python SDK reference |
| [TypeScript API](./typescript-api) | TypeScript SDK reference |
| [CLI Reference](./cli-reference) | Command-line interface |

## Quick Links

### Memory Operations
- `store_memory(content, **options)` - Store new memory
- `query_memories(query, **options)` - Search memories
- `list_memories(**options)` - List all memories
- `get_memory(id)` - Get specific memory
- `update_memory(id, **updates)` - Update memory
- `delete_memory(id)` - Delete memory

### Block Operations
- `get_subconscious_block(label)` - Get block content
- `update_subconscious_block(label, content)` - Update block
- `add_subconscious_guidance(line)` - Add guidance line

### Hook Operations
- `register_hook(name, event_type, url, **options)` - Register hook
- `list_hooks()` - List all hooks
- `unregister_hook(hook_id)` - Remove hook

### WebSocket Operations
- `ws_subscribe(subscription_id, event_types, **options)` - Subscribe
- `ws_unsubscribe(subscription_id)` - Unsubscribe
- `ws_status()` - Get status

## Versioning

API follows semantic versioning. Breaking changes will increment the major version.
