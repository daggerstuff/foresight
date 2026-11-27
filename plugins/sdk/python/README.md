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
import contextlib

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
| `ForesightClient` | `inject_context` (`/ui/api/inject`), `capture_text` (`/ui/api/capture`), `capture_transcript` (`/session/transcript`), `post_telemetry`, `call_tool` (MCP `/mcp`) |

## Error contract

The low-level helpers raise and never return partial data:
`post_json` and `ForesightClient.post` raise `ForesightHTTPError` on a
`>= 400` status and `ForesightError` on transport failure, an invalid URL, or
a cleartext-HTTP request that would carry the API key off loopback.

The high-level helpers are fail-safe: `inject_context`, `capture_text`,
`capture_transcript` and `post_telemetry` suppress `ForesightError` and
return their documented `None`/empty value, and `call_tool` returns `None` on
transport or tool failure. An unreachable Foresight server therefore cannot
break the host agent session, and an exception handler is *not* a reliable way
to detect a failed high-level call — check the return value. Hook callers that
call the low-level helpers directly must wrap them in
`contextlib.suppress(Exception)`; see `plugins/claude/hooks/foresight_hook.py`
for the reference pattern (SDK import with inline fallbacks when the file
isn't installed).

## Server discovery

`FORESIGHT_MCP_URL` → `FORESIGHT_URL` → credentials file →
`http://127.0.0.1:8764`. Remote/hosted deployments set `FORESIGHT_API_KEY`
(Bearer) and `FORESIGHT_IDENTITY`/`FORESIGHT_USER_ID` (lane).
