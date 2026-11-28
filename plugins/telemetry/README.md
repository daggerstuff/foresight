# Foresight visible-benefit standard & plugin telemetry (FOR-70)

A plugin is only valuable if the user can *tell* Foresight helped. This spec
defines (1) the marker conventions plugins use to surface benefit, and
(2) the telemetry event contract for reporting it — without leaking memory
contents or PHI.

## 1. Benefit markers

Markers are short, single-purpose signals. They must be **counts and status
only by default** — see §3 privacy rules.

| Marker kind | Where it appears | Format |
| --- | --- | --- |
| **Recall badge / status line** | Agent status line, hook stdout preamble, plugin UI chip | `[FORESIGHT] recall: <n> memories, <m> blocks · <latency>ms` |
| **Injection block** | Prompt/context injection (already shipped) | `[FORESIGHT CONTINUITY CONTEXT] … [/FORESIGHT CONTINUITY CONTEXT]` |
| **Capture confirmation** | Hook `systemMessage`, toast, status line | `[FORESIGHT] captured <n> items (<kinds>)` — counts/kinds only |
| **Write-back nudge** | Hook `systemMessage` JSON (Claude Stop convention) | `{"systemMessage": "[FORESIGHT WRITE-BACK] …"}` |
| **Doctor/status line** | `foresight doctor`, plugin health UI | `foresight: plugin=<id> surface=<s> events_24h=<n> last_error=<bool>` |
| **Gated recall notice** | When gated recall suppresses injection (FOR-1) | `[FORESIGHT] recall gated (score <threshold) — directives only` |

Rules for all markers:

- Prefix machine-readable markers with `[FORESIGHT` so hosts/users can grep
  and style them; keep them to **one line** unless the marker is an injection
  block.
- Show **counts, kinds, scores, latency** — e.g. `3 memories, 1 block, 42ms`.
- Never print memory bodies, prompts, or transcript text in a marker.
- Titles are opt-in only (see §3).
- When Foresight is unreachable or returns nothing, emit **no marker** —
  silence is the correct fail-safe (plugins must not nag).

Sample artifacts live in `samples/` (status line, hook systemMessage,
CLI doctor output, plugin UI badge).

## 2. Telemetry events

Endpoint: `POST /ui/api/telemetry/event` (same auth as `/ui/api/*` —
Bearer/`X-API-Key` + `X-User-Id`/`X-Tenant-Id`). Schema:
[`event.schema.json`](event.schema.json). SDK helpers: `track_event`
(Python) / `trackEvent` (TypeScript) in `plugins/sdk/`.

Payload:

```json
{
  "event": "recall.injected",
  "plugin": "claude",
  "surface": "claude-code",
  "latency_ms": 42.5,
  "fields": { "memories_injected": 3, "blocks_injected": 1, "top_score": 0.62 }
}
```

### Event catalog

| Event | When | Suggested `fields` |
| --- | --- | --- |
| `recall.injected` | Context/memories injected into a turn | `memories_injected`, `blocks_injected`, `top_score`, `latency_ms` |
| `recall.gated` | Gated recall suppressed injection (pairs with `/ui/api/telemetry/gating`) | `top_score`, `threshold` |
| `recall.empty` | Recall ran but returned nothing | `latency_ms` |
| `capture.stored` | In-flight/end-of-turn capture accepted | `items_stored`, `kinds` (e.g. `"decision,preference"`) |
| `capture.failed` | Capture call failed (server down, HTTP error) | `error_kind` (`timeout`,`http_5xx`,`unreachable`) |
| `plugin.health` | Periodic plugin self-check | `ok` (bool), `server_version`, `last_error_kind` |
| `plugin.installed` / `plugin.uninstalled` | Install/uninstall lifecycle | `install_method` (`script`,`manual`,`mcp`) |
| `tool.called` | Plugin invoked an MCP tool on the user's behalf | `tool` (name only), `latency_ms` |

Event names: `^[a-z][a-z0-9_.]{0,63}$`. `plugin` should be the manifest `id`.

Server-side guarantees (`foresight/telemetry.py::record_plugin_event`):

- `fields` is sanitized to ≤12 scalar keys; strings truncated to 128 chars;
  nested objects/lists dropped; non-finite numbers dropped.
- Events aggregate into `plugin_event_counts` + a 100-entry recent ring,
  surfaced under `plugin_events` in `GET /ui/api/telemetry`.

## 3. Privacy rules

- **Counts, kinds, scores, latency** are always safe.
- **Titles** may appear in markers only when the plugin declares opt-in
  (`FORESIGHT_MARKERS_SHOW_TITLES=1` or manifest capability
  `benefit.show-titles`); default is counts only.
- **Never** send memory bodies, prompts, transcript text, file paths
  containing usernames, or clinical/PHI-adjacent content in `fields`.
  The server truncates strings but cannot detect semantic PHI — plugins
  must apply judgment; when in doubt, send counts only.
- `fields` values are persisted to `~/.config/foresight/telemetry.json`
  locally and aggregated in `/ui/api/telemetry`; treat them as low-sensitivity
  metadata, not a content channel.
- Gated-recall markers (FOR-1) appear **only when recall clears the
  confidence bar** — a gated turn shows the notice marker, not memories.
