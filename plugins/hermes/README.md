# Foresight Memory Provider for Hermes Agent

Plugs the shared Foresight brain into Hermes Agent as a first-class
[`MemoryProvider`](https://github.com/NousResearch/hermes-agent): automatic
recall each turn, transcript distillation, and `foresight_*` tools.

## Install

```bash
bash plugins/hermes/install.sh            # symlinks into ~/.hermes/plugins/foresight
bash plugins/hermes/install.sh --copy     # copy instead
bash plugins/hermes/install.sh --hermes-home /path/to/.hermes
```

Then select `foresight` as the memory provider (`hermes memory setup` or the
dashboard) and restart the agent.

## Configuration

All settings are environment variables (profile-scoped secrets supported):

| Variable            | Default                  | Purpose                                  |
| ------------------- | ------------------------ | ---------------------------------------- |
| `FORESIGHT_URL`     | `http://127.0.0.1:8764`  | Foresight REST base URL                  |
| `FORESIGHT_API_KEY` | —                        | Bearer key when the server requires auth |
| `FORESIGHT_USER_ID` | server default           | `X-User-Id` identity lane                |

Point `FORESIGHT_URL` at `https://foresight.pixelated.love` to share the
fleet-wide brain; the API key comes from `foresight user create` on the server.

## What it does

- **`queue_prefetch` / `prefetch`** — background `POST /inject` recall each
  turn; cached result injected as `[Foresight Memory]` context.
- **`sync_turn`** — each completed turn is sent to `POST /session/transcript`
  for distillation (background thread, non-blocking).
- **`on_session_end`** — full transcript sent for end-of-session extraction.
- **`on_memory_write`** — mirrors built-in memory-tool `add` writes into the
  shared brain.
- **Tools** — `foresight_search`, `foresight_remember`, `foresight_context`,
  `foresight_forget`, `foresight_list` mapped to the REST API.

## Files

- `plugin.yaml` — provider manifest
- `__init__.py` — `ForesightMemoryProvider` + `register(ctx)`
- `install.sh` — installer
