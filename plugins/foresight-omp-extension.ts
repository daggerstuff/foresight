declare const process: {
  env: Record<string, string | undefined>
}

const FORESIGHT_URL: string =
  process.env.FORESIGHT_HTTP_URL ?? 'http://127.0.0.1:8764'

interface InjectResponse {
  formatted?: string
}

interface OmpContext {
  sessionId?: string
  session?: { id?: string }
  messages?: unknown[]
  injectContext?: (text: string) => void
  appendSystemPrompt?: (text: string) => void
}

interface OmpAgentEndEvent {
  messages?: unknown[]
}

interface OmpPluginApi {
  on(
    event: 'agent_start',
    handler: (_event: unknown, ctx: OmpContext) => Promise<void> | void,
  ): void
  on(
    event: 'agent_end',
    handler: (event: OmpAgentEndEvent, ctx: OmpContext) => Promise<void> | void,
  ): void
  on(event: string, handler: (...args: unknown[]) => Promise<void> | void): void
}

async function postJson<T = unknown>(
  path: string,
  payload: unknown,
  timeoutMs = 12000,
  extraHeaders: Record<string, string> = {},
): Promise<T | null> {
  try {
    const url = new URL(path, FORESIGHT_URL)
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'MCP-Protocol-Version': '2026-07-28',
        'Mcp-Method': 'tools/call',
        ...extraHeaders,
      },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs),
    })
    if (!response.ok) {
      return null
    }
    return (await response.json()) as T
  } catch {
    return null
  }
}

export default function foresightOmpPlugin(pi: OmpPluginApi) {
  // 1. When agent starts, retrieve and inject relevant context
  pi.on('agent_start', async (_event: unknown, ctx: OmpContext) => {
    try {
      const res = await postJson<InjectResponse>('/ui/api/inject', {
        text: 'active session goals and user preferences',
      })
      if (res?.formatted && ctx) {
        if (typeof ctx.injectContext === 'function') {
          ctx.injectContext(
            `[FORESIGHT CONTEXT]\n${res.formatted}\n[/FORESIGHT CONTEXT]`,
          )
        } else if (typeof ctx.appendSystemPrompt === 'function') {
          ctx.appendSystemPrompt(
            `[FORESIGHT CONTEXT]\n${res.formatted}\n[/FORESIGHT CONTEXT]`,
          )
        }
      }
    } catch {
      // Non-blocking failure tolerance
    }
  })

  // 2. When agent finishes, asynchronously process transcript for memory capture
  pi.on('agent_end', async (event: OmpAgentEndEvent, ctx: OmpContext) => {
    try {
      const sessionId = ctx?.sessionId ?? ctx?.session?.id ?? 'omp-session'
      const messages = ctx?.messages ?? event?.messages ?? []
      if (messages.length > 0) {
        void postJson(
          '/mcp',
          {
            jsonrpc: '2.0',
            id: Date.now(),
            method: 'tools/call',
            params: {
              name: 'process_session_transcript',
              arguments: {
                session_id: sessionId,
                messages: messages.slice(-10),
              },
              _meta: {
                'io.modelcontextprotocol/protocolVersion': '2026-07-28',
                'io.modelcontextprotocol/clientCapabilities': {},
              },
            },
          },
          12000,
          { 'Mcp-Name': 'process_session_transcript' },
        )
      }
    } catch {
      // Non-blocking fire-and-forget
    }
  })
}
