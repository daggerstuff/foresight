---
sidebar_label: CLI Reference
title: CLI Reference
---

# CLI Reference

Command-line interface for Foresight memory operations, context blocks, and
curation runs.

## Installation

```bash
# Run the installed CLI
uv run foresight --help

# Or use the compatibility wrapper
uv run python scripts/foresight-cli.py --help
```

## Global options

| Option            | Description                               | Default |
| ----------------- | ----------------------------------------- | ------- |
| `--help`          | Show help                                 | -       |
| `--json`          | Emit machine-readable JSON when supported | `false` |
| `--user-id`, `-u` | Override the active user ID               | auto    |

## Memory commands

```bash
foresight store <content> [options]
foresight query <query> [options]
foresight list [options]
foresight get <memory_id>
foresight update <memory_id> [options]
foresight delete <memory_id>
foresight synthesize [options]
foresight reflect [options]
foresight diff <memory_id> <version1> <version2>
foresight rollback <memory_id> <version>
foresight status
```

## Context block commands

```bash
foresight blocks list
foresight blocks get <label>
foresight blocks update <label> <content>
foresight blocks reset <label>
foresight blocks clear <label>
```

Common labels include `guidance`, `pending_items`, `project_context`,
`session_patterns`, and `user_preferences`.

## Curation commands

```bash
foresight curate create --source-bank-id <bank> [options]
foresight curate get <run_id>
foresight curate list [options]
foresight curate cancel <run_id>
foresight curate archive <run_id>
```

### `foresight curate create` options

| Option                     | Description                                           | Default             |
| -------------------------- | ----------------------------------------------------- | ------------------- |
| `--source-bank-id`         | Source bank to curate                                 | required            |
| `--output-bank-id`         | Optional destination bank override                    | auto                |
| `--policy-mode`            | `preserve`, `rebalance`, or `rebuild`                 | `rebalance`         |
| `--tool-access`            | `disabled`, `observe`, or `operate`                   | `observe`           |
| `--output-mode`            | `reviewable_output` or `in_place`                     | `reviewable_output` |
| `--instructions`           | Curator guidance for this run                         | none                |
| `--transcript-bundle-file` | JSON transcript bundle to fold into curation          | none                |
| `--session-id`             | Optional session identifier for the transcript bundle | none                |
| `--project-path`           | Optional project path for the transcript bundle       | none                |

## Examples

```bash
# Store a memory
foresight store "User prefers concise updates" --category preference

# Inspect context blocks
foresight blocks list
foresight blocks get guidance
foresight blocks update guidance "Keep updates short and concrete."

# Create a reviewable curation run
foresight curate create   --source-bank-id default   --policy-mode rebalance   --tool-access observe   --output-mode reviewable_output   --instructions "Merge duplicates and preserve durable preferences"

# Create a transcript-aware in-place run
foresight curate create   --source-bank-id default   --tool-access operate   --output-mode in_place   --transcript-bundle-file /tmp/transcript-bundle.json

# Inspect run state
foresight curate list
foresight curate get cur_abc123def456
foresight curate cancel cur_abc123def456
```

## Migration note

Older documentation may mention `foresight subconscious ...` commands. Those
names have been replaced on the public CLI by `foresight blocks ...`, with a
hidden compatibility alias only for legacy automation.
