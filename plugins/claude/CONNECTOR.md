# Foresight on claude.ai web & mobile

The plugin (`plugins/claude/`) only works in **Claude Code** and **Claude
Desktop** — hooks, skills, and `.mcp.json` don't exist on claude.ai. On the
web/mobile app the only option is a **custom MCP connector**, which gives you
the Foresight tools but none of the automatic context injection or capture.

## Requirements

claude.ai connectors need a **public HTTPS URL**. `127.0.0.1:8764` is
localhost-only, so expose the server first, and keep auth enabled — never
expose Foresight unauthenticated.

### Option A: Tunnel (quickest)

```bash
# Cloudflare Tunnel (named tunnel or quick tunnel)
cloudflared tunnel --url http://127.0.0.1:8764

# or ngrok
ngrok http 8764
```

This gives you `https://<something>.trycloudflare.com` (or your ngrok URL).
The MCP endpoint is `<url>/mcp`.

> Quick tunnels are ephemeral — the URL changes each run. For anything
> semi-permanent, use a named Cloudflare Tunnel or deploy Foresight properly.

### Option B: Deploy

Run `foresight-server --host 0.0.0.0 --port 8764` behind your own HTTPS
reverse proxy (Caddy/nginx/ALB) with a real domain.

## Add the connector

1. claude.ai → **Settings → Connectors → Add custom connector**
   (on mobile: profile → Settings → Connectors).
2. URL: `https://<your-host>/mcp`
3. If prompted for auth, use your Foresight API key as a Bearer token
   (claude.ai connector auth support varies by client — if it can't send
   headers, terminate auth at your proxy/tunnel instead, e.g. Cloudflare
   Access or ngrok's auth, and keep `FORESIGHT_ALLOW_UNAUTHENTICATED` off
   by having the proxy inject the `Authorization` header).

## What you get vs. the plugin

| Feature | Plugin (Code/Desktop) | Connector (web/mobile) |
| --- | --- | --- |
| MCP tools (`search_memories`, `manage_memories`, …) | ✅ | ✅ |
| Auto context injection per prompt | ✅ | ❌ |
| Transcript capture on stop/compact | ✅ | ❌ |
| Foresight skill | ✅ | ❌ |

The agent can still call `inject_context` manually each session if you ask it
to — it's just not automatic.
