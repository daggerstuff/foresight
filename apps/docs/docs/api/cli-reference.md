---
sidebar_label: CLI Reference
title: CLI Reference
---

# CLI Reference

Command-line interface for Foresight operations.

## Commands

### Memory Commands

```bash
# Store a memory
foresight store <content> [options]

# Get specific memory
foresight get <memory_id> [options]

# List memories
foresight list [options]

# Query memories
foresight query <query> [options]

# Update memory
foresight update <memory_id> [options]

# Delete memory
foresight delete <memory_id> [options]

# Synthesize memories
foresight synthesize [options]

# Archive memory
foresight archive <memory_id> [options]
```

### Block Commands

```bash
# List block schemas
foresight block list [options]

# Create block
foresight block create <label> [options]

# Get block
foresight block get <label> [options]
```

### Hook Commands

```bash
# List hooks
foresight hook list [options]

# Register hook
foresight hook register <name> <event_type> [options]

# Unregister hook
foresight hook unregister <hook_id> [options]
```

### Event Commands

```bash
# View event log
foresight event log [options]
```

### System Commands

```bash
# Get status
foresight status [options]
```

## Options

### Global Options

| Option | Description |
|--------|-------------|
| `--help` | Show help |
| `--json` | Output as JSON |
| `--user-id` | User ID override |

### Memory Options

| Option | Description | Default |
|--------|-------------|---------|
| `--scope` | session, arc, trait, fact | session |
| `--retention` | ephemeral, short_term, long_term, permanent | short_term |
| `--category` | Category label | fact |
| `--limit` | Number of results | 10 |
| `--offset` | Offset for pagination | 0 |

### Hook Options

| Option | Description | Default |
|--------|-------------|---------|
| `--url` | Webhook URL | Required |
| `--retry` | Retry count | 3 |
| `--timeout` | Timeout (seconds) | 30 |

## Examples

```bash
# Store with options
foresight store "My memory" --scope fact --retention permanent

# Query with limit
foresight query "test" --limit 5

# JSON output
foresight status --json

# List with pagination
foresight list --limit 20 --offset 40
```

## Related

- [Python API](./python-api)
- [TypeScript API](./typescript-api)
