# Foresight agent plugins

Native integrations that wire Foresight memory into coding agents. Every plugin
directory ships a `foresight.plugin.json` manifest describing what it installs,
which agent targets it supports, which Foresight capabilities it requires, and
how to install/uninstall it.

## The manifest contract

`foresight.plugin.json` (specVersion 1) is validated by
`foresight/plugin_manifest.py`. The canonical JSON Schema is
[`manifest.schema.json`](manifest.schema.json) — regenerate it with:

```bash
uv run python -m foresight.plugin_manifest --write-schema plugins/manifest.schema.json
```

Validate everything under `plugins/`:

```bash
uv run python -m foresight.plugin_manifest plugins/
```

### Required fields

| Field | Meaning |
| --- | --- |
| `specVersion` | Contract version; currently only `1`. |
| `id` | Stable slug (`^[a-z0-9][a-z0-9-]*$`), e.g. `claude`. |
| `name`, `version` | Human name + plugin semver (independent of server version). |
| `targets` | Agent surfaces this plugin loads in (see matrix below). |
| `surfaces` | Capability surfaces implemented: `mcp`, `rest`, `hooks`, `skills`, `rules`, `extension`, `provider`, `status-ui`. |
| `requires.foresight` | Server version constraint, e.g. `">=0.19.0"`. |
| `requires.transports` | `http`, `stdio`, `sse` — subset of what the plugin can speak. |
| `requires.capabilities` | Foresight capabilities used: `context.inject`, `context-blocks`, `capture.transcript`, `capture.in-flight`, `memory.search`, `memory.store`, `memory.update`, `memory.delete`, `memory.reflect`, `profile.read`, `profile.write`, `status.health`, `status.doctor`, `documents.ingest`, `temporal.query`, `curation.manage`, `telemetry.gating`. |

### Optional fields

- `artifacts[]` — every file the plugin installs: `type` (`config`, `hook`,
  `skill`, `rule`, `extension`, `provider`, `cli`, `docs`, `native-manifest`,
  `source`), repo-relative `path` (no absolute paths or `..`), optional
  `surface`, per-`targets` overrides, and `description`.
- `credentials` — **references only**: `envFile` (e.g.
  `~/.config/foresight/credentials.env`, mode 600) and `keys` (env var names).
  Never embed secrets.
- `install` / `uninstall` — command + `dryRunFlag`, or notes for manual removal.
- `author`, `license`, `homepage`, `repository`, `keywords`, `description`.

### Identity & lanes

- Local server default: `http://127.0.0.1:8764` (`/mcp` for MCP, `/ui/api/*`
  and REST routes for clients).
- Remote/hosted: `Authorization: Bearer <key>` plus `X-User-Id: <lane>`;
  OAuth-capable endpoints exist for web connectors (see
  `plugins/claude/CONNECTOR.md`).
- Tenant/user isolation is enforced server-side via contextvars; plugins must
  not pass tenant ids as loose arguments.

## Shared SDKs

Plugins should not hand-roll HTTP, credential loading, or hook payload
parsing — use the shared SDKs in `plugins/sdk/`:

- **`sdk/python/foresight_plugin_sdk.py`** — single-file stdlib SDK for
  Python hooks/providers. Installers copy it next to the hook (see
  `plugins/claude/install.sh`); hooks import it as a sibling with inline
  fallbacks. API + fail-safe contract: `sdk/python/README.md`.
- **`sdk/typescript/`** (`@foresight/plugin-sdk`) — TS SDK for JS/TS plugin
  targets; ships raw `.ts` like the MastraCode plugin. API + fail-safe
  contract: `sdk/typescript/README.md`.

Both resolve server discovery (`FORESIGHT_MCP_URL` → `FORESIGHT_URL` →
`http://127.0.0.1:8764`), credentials (`~/.config/foresight/credentials.env`,
mode 600, env-first precedence), and auth headers (Bearer + `X-User-Id`
lane). All high-level helpers are fail-safe: an unreachable server must
never break the host agent session.

Current consumers: `plugins/claude/hooks/foresight_hook.py` (Python) and
`plugins/mastracode/src/client.ts` (TypeScript re-export shim).

## Capability matrix

Status: ✅ native plugin · 🔌 MCP/REST wiring only · 📋 planned (P-FOR-58) ·
— not applicable.

| Agent target | MCP | REST/hooks | Skills/rules | Extension/provider | Status UI | Transcript capture | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `claude-code` | ✅ `.mcp.json` | ✅ hooks | ✅ `SKILL.md` | — | — | ✅ Stop/SessionEnd | `plugins/claude` |
| `claude-desktop` | ✅ stdio/http | — | — | — | — | — | same plugin, `--desktop` |
| `hermes` | — | ✅ REST | — | ✅ provider | ✅ `recall_status` | ✅ sync_turn/pre_compress/session_end | `plugins/hermes` |
| `mastracode` | — | ✅ REST | — | ✅ TS plugin | 📋 | ✅ processOutputResult | `plugins/mastracode` |
| `opencode` | ✅ | ✅ autoinject | — | ✅ JS plugin | — | 📋 | `foresight-autoinject.js` (audit FOR-72) |
| `omp` (Oh My Pi) | ✅ | ✅ | — | ✅ TS extension | — | 📋 | `foresight-omp-extension.ts` (audit FOR-72) |
| `copilot` | — | ✅ sessionStart hook | — | — | — | — | `foresight-copilot-hook.json` (audit FOR-72) |
| `gemini-cli` / `antigravity` | 📋 | 📋 | 📋 `GEMINI.md` | 📋 | — | 📋 | FOR-74 |
| `codex` | 📋 | 📋 | 📋 `AGENTS.md` | 📋 | — | 📋 | FOR-75 |
| `omo` | 📋 | 📋 | 📋 | 📋 | 📋 | 📋 | FOR-76 |
| `deepseek-harness` | 📋 | 📋 | 📋 | 📋 dsh-market | 📋 | 📋 | FOR-77 (pairs with FOR-29) |
| `pi` | 📋 | 📋 | 📋 | 📋 | — | 📋 | FOR-78 |
| `ohmypi` | 📋 | 📋 | 📋 | 📋 | — | 📋 | FOR-79 (confirm vs `pi`) |
| `devin` | 📋 | 📋 | 📋 skills | 📋 | — | 📋 | FOR-81 |
| `cursor` | 📋 | 📋 | 📋 rules | 📋 | — | 📋 | FOR-82 |
| `goose` | 📋 | 📋 | 📋 | 📋 | — | 📋 | FOR-83 |
| `generic-mcp` | ✅ | — | — | — | — | — | any MCP client; config generator FOR-84 |
| `web` | — | ✅ hosted REST/OAuth | — | — | — | — | connector fallback; FOR-85 |

## Adding a plugin

1. Create `plugins/<agent>/` with the native artifacts the agent actually
   loads (manifest, hooks, skills, extension — not just config snippets).
2. Add `foresight.plugin.json` declaring `targets`, `surfaces`, `requires`,
   `artifacts`, `credentials`, `install`/`uninstall`.
3. `uv run python -m foresight.plugin_manifest plugins/<agent>` must pass.
4. Add/refresh the row in the capability matrix above.
5. Prove functional MCP/REST usage + a visible benefit marker in the
   conformance harness (FOR-71) before marking done.
