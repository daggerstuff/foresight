# Foresight Memory Provider for Hermes Agent

Plugs the shared Foresight brain (`https://foresight.pixelated.love` or a local
`foresight.service`) into Hermes Agent as a first-class memory provider.

## What it does (Hands-Off Operation)

- **Hands-off Proactive Recall:** Synchronous, relevance-ranked memories,
  context blocks, directives, and user preferences are recalled and injected
  before the model begins responding on every non-trivial turn.
- **Deterministic UI Recall Indicator:** Emits `🔮 Foresight — recalled X memories`
  via `recall_status()` so the user immediately sees memory working on every turn.
- **Continuous Hands-off Distillation:**
  - `sync_turn` — completed turns are distilled into the shared brain in the
    background using `spawn_context_thread` (non-blocking).
  - `on_pre_compress` — API v2 fail-closed checkpoint ensures no memories or
    decisions are lost when Hermes compresses conversation history.
  - `on_session_end` — comprehensive end-of-session distillation.
  - `on_delegation` — stores subagent tasks and findings in memory.
  - `on_memory_write` — mirrors built-in memory-tool writes (`add`, `replace`,
    `remove`) into the shared brain.
- **Rich Memory Tools:**
  - `foresight_search` — hybrid semantic + keyword search with relevance ranking.
  - `foresight_remember` — store durable facts, preferences, decisions, and patterns.
  - `foresight_reflect` — synthesize multi-hop reasoning across memories.
  - `foresight_profile` — retrieve user preferences, persistent traits, and mental models.
  - `foresight_context` — retrieve injected context for a specific topic.
  - `foresight_list` — list recent memories.
  - `foresight_forget` — delete memories by ID.
- **Hermes CLI Subcommands:** Direct `hermes foresight ...` CLI commands for
  status, inspection, reflection, and self-testing.
- **Hermes Desktop / Web UI Panel:** Declarative `config_schema.py` for UI config.

## Install

```bash
# Local install (symlinks into ~/.hermes/plugins/foresight)
bash plugins/hermes/install.sh

# Or deploy directly to a fleet host via SSH
bash plugins/hermes/install.sh --remote hermes
```

Then activate Foresight as the memory provider:

```bash
hermes memory setup foresight
# or verify active provider
hermes memory status
```

## Configuration

Settings can be set in `~/.hermes/.env` (profile-scoped) or in `config.yaml`:

| Variable / Key      | Default                 | Purpose                                  |
| ------------------- | ----------------------- | ---------------------------------------- |
| `FORESIGHT_URL`     | `http://127.0.0.1:8764` | Foresight REST base URL                  |
| `FORESIGHT_API_KEY` | —                       | Bearer key when the server requires auth |
| `FORESIGHT_USER_ID` | profile identity / chad | `X-User-Id` identity lane                |

To share the fleet-wide brain, set `FORESIGHT_URL=https://foresight.pixelated.love`.

## CLI Subcommands

```bash
# Check connectivity, latency, memory counts, and recent memories
hermes foresight status

# Search memory using hybrid semantic + keyword search
hermes foresight search "fireworks training"

# Store an explicit memory
hermes foresight remember "Adaption AI train uses export_adaption_release" --category decision

# Synthesize reasoning across disparate memories
hermes foresight reflect "What accounts are unblocked for training?"

# View synthesized user profile, preferences, and mental models
hermes foresight profile

# Run an end-to-end integration test
hermes foresight test
```

## Files

- `__init__.py` — `ForesightMemoryProvider` implementation with full lifecycle hooks.
- `cli.py` — `hermes foresight ...` CLI subcommands.
- `config_schema.py` — Hermes Desktop / Web UI capabilities panel schema.
- `plugin.yaml` — Provider manifest and hook registrations.
- `install.sh` — Local installer and remote fleet deployer.
