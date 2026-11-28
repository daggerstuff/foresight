# Foresight plugin SDK — Python

Single-file, stdlib-only SDK for Python hook/provider targets (Claude
hooks, Hermes provider, DSH plugin, generic hooks). Version `0.1.0`
(`__version__`).

## Usage

The module is `foresight_plugin_sdk.py`. Two consumption patterns:

- **Installed plugins**: the installer copies this file next to the hook
  (see `plugins/claude/install.sh`); the hook imports it as a sibling.
- **Repo layout**: importable from `plugins/sdk/python/` — add that dir to
  `sys.path` or vendor the file.

```python
import foresight_plugin_sdk as sdk

client = sdk.ForesightClient.from_env()
with contextlib.suppress(Exception):
    block = client.inject_context(prompt_text)
```

## What it provides

| Helper | Purpose |
| --- | --- |
| `resolve_credentials` / `Credentials` | env → `~/.config/foresight/credentials.env` resolution; `lane` derives `X-User-Id` from `FORESIGHT_IDENTITY` (`alice@acme` → `alice`) |
| `auth_headers` | `Content-Type` + `Authorization: Bearer` + `X-User-Id` |
| `post_json` | stdlib `http.client` POST; raises `ForesightHTTPError` (>=400) / `ForesightError` (transport) |
| `read_hook_input` | stdin JSON hook payload → `dict` (`{}` on failure) |
| `parse_sse_result` / `mcp_result_text` | MCP `tools/call` response flattening |
| `ForesightClient` | `inject_context` (`/ui/api/inject`), `capture_text` (`/ui/api/capture`), `capture_transcript` (`/session/transcript`), `post_telemetry`, `track_event` (`/ui/api/telemetry/event`, FOR-70), `call_tool` (MCP `/mcp`) |

## Fail-safe contract

All helpers raise `ForesightError` subclasses on failure and never return
partial data. Hook callers must wrap calls in `contextlib.suppress(Exception)`
so an unreachable Foresight server can never break the host agent session —
see `plugins/claude/hooks/foresight_hook.py` for the reference pattern
(SDK import with inline fallbacks when the file isn't installed).

## Server discovery

`FORESIGHT_MCP_URL` → `FORESIGHT_URL` → credentials file →
`http://127.0.0.1:8764`. Remote/hosted deployments set `FORESIGHT_API_KEY`
(Bearer) and `FORESIGHT_IDENTITY`/`FORESIGHT_USER_ID` (lane).
