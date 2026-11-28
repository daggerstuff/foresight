# @foresight/plugin-sdk — TypeScript

Shared Foresight client for JS/TS plugin targets (MastraCode, OMP, OpenCode,
Cursor/Codex snippets). Version `0.1.0`. Ships raw TypeScript (`exports:
./src/index.ts`) like the MastraCode plugin — the host transpiles.

## Consumption

In-repo plugins import the source directly:

```ts
import { fetchInjectContext, mcpCall } from '../../sdk/typescript/src/index.js'
```

(`client.ts` in `plugins/mastracode` is the reference re-export shim.)

For external distribution the package can be published to npm or vendored;
see FOR-90 (unified installer/distribution).

## API

| Export | Purpose |
| --- | --- |
| `resolveBaseUrl` / `defaultCredentialsPath` | server + credentials.env discovery (`FORESIGHT_MCP_URL` → `FORESIGHT_URL` → `http://127.0.0.1:8764`) |
| `parseEnvFile` / `loadCredentials` | KEY=VALUE env file + env resolution → `ForesightCredentials` (`lane` = identity minus `@domain`) |
| `authHeaders` | `Content-Type` + `Authorization: Bearer` + `X-User-Id` |
| `postJson` / `PostJsonResult` | fetch POST with `AbortController` timeout; resolves `{status, body}` for any HTTP status, rejects only on transport failure |
| `parseSSEResult` | JSON or `data:`-framed JSON-RPC result extraction |
| `mcpCall` | MCP `tools/call` → joined text content or `null` |
| `fetchInjectContext` | `/ui/api/inject` REST first, MCP `inject_context` fallback |
| `captureText` / `captureTranscript` | `/ui/api/capture` + `/session/transcript` |
| `postTelemetry` | `/ui/api/telemetry/gating` |
| `trackEvent` | `/ui/api/telemetry/event` plugin benefit/health events (FOR-70); scalar `fields` only |
| `autoCaptureTurn` | fire-and-forget `process_session_transcript` distillation |
| `ForesightError` | thrown only by low-level `postJson` transport failures |

## Fail-safe contract

High-level helpers (`fetchInjectContext`, `mcpCall`, `captureText`, …)
resolve to `null`/`false` on any failure — a down Foresight server must
never break the host agent. `postJson` is the only throwing primitive.

## Develop

```bash
pnpm install
pnpm vitest run   # 17 tests, fetch mocked
pnpm exec tsc --noEmit
```
