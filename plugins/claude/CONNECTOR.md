# Foresight on claude.ai web & mobile

The plugin (`plugins/claude/`) only works in **Claude Code** and **Claude
Desktop** — hooks, skills, and `.mcp.json` don't exist on claude.ai. On the
web/mobile app the only option is a **custom MCP connector**, which gives you
the Foresight tools but none of the automatic context injection or capture.

## You already have a public endpoint

Foresight is exposed publicly via Cloudflare Tunnel with full OAuth:

- **MCP URL:** `https://foresight.pixelated.love/mcp`
- **Health:** `https://foresight.pixelated.love/health`
- Served by the `foresight-oauth.service` systemd user unit (local upstream
  `127.0.0.1:8765`, `FORESIGHT_REQUIRE_API_KEY=1`).
- OAuth metadata is live:
  `/.well-known/oauth-protected-resource` and
  `/.well-known/oauth-authorization-server` (authorization code + PKCE,
  dynamic client registration at `/register`).

The fleet nodes (`billy`, `gnasty`) share the same brain through the common
Neon Postgres backend — this public endpoint fronts the same store.

## Add the connector (web & mobile)

1. claude.ai → **Settings → Connectors → Add custom connector**
   (mobile: profile → Settings → Connectors).
2. URL: `https://foresight.pixelated.love/mcp`
3. Complete the OAuth flow when prompted — approve access in the browser.
   Claude registers itself via dynamic client registration; no API key goes
   in the config.

That's it — no tunnel or deploy step needed on the client side.

## What you get vs. the plugin

| Feature | Plugin (Code/Desktop) | Connector (web/mobile) |
| --- | --- | --- |
| MCP tools (`search_memories`, `manage_memories`, …) | ✅ | ✅ |
| Auto context injection per prompt | ✅ | ❌ |
| Transcript capture on stop/compact | ✅ | ❌ |
| Foresight skill | ✅ | ❌ |

The agent can still call `inject_context` manually each session if you ask it
to — it's just not automatic.

## Self-hosting notes (other deployments)

If you're setting this up on a different Foresight instance without a public
endpoint, see the tunnel/deploy options below — or just reuse the pattern:
`cloudflared` named tunnel → `foresight-oauth.service` env block
(`FORESIGHT_OAUTH_BASE_URL`, static client vars) → `https://<domain>/mcp`.
