# Mastra Code Plugin: Foresight Persistent Memory

Native TypeScript plugin for **Mastra Code** providing hands-off continuous
memory, context injection, and background turn distillation via the Foresight
engine.

## Features

- **Zero-Touch Context Injection (`processInput`)**: Automatically queries
  Foresight before every user turn and injects relevant standing decisions,
  architecture context, and user preferences into the agent's prompt.
- **Background Auto-Capture (`processOutputResult`)**: Silently extracts key
  technical facts, architecture decisions, and preferences from completed turns
  without interrupting the user.
- **Native Tools**:
  - `foresight_inject_context`: On-demand context retrieval for new topics or
    planning.
  - `foresight_store_memory`: Explicit persistence of important decisions or
    rules.
  - `foresight_search_memories`: Semantic and keyword search over past memories.
  - `foresight_get_context_blocks`: View active distilled context blocks (e.g.
    `user_preferences`).
  - `foresight_update_context_block`: Update or refine standing context blocks.
  - `foresight_system_status`: Inspect connection health and memory telemetry.
- **Configurable**: Adjust server URL, user ID, and toggles for auto-inject and
  auto-capture via Mastra Code's `/plugins` interface.

## Installation

### Method 1: Local Link in Mastra Code

Open Mastra Code, type:

```text
/plugins
```

Choose **Install new plugin** → **Local path** → Enter:

```text
/home/vivi/pixelated/foresight/plugins/mastracode
```

### Method 2: Global Configuration (`~/.mastracode/plugins/plugins.json`)

Add to `~/.mastracode/plugins/plugins.json`:

```json
{
  "plugins": {
    "foresight": {
      "enabled": true,
      "source": "local",
      "specifier": "/home/vivi/pixelated/foresight/plugins/mastracode",
      "path": "/home/vivi/pixelated/foresight/plugins/mastracode",
      "entry": "src/index.ts",
      "version": "1.0.0"
    }
  }
}
```
