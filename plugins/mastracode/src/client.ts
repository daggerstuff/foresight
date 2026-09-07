/**
 * Foresight HTTP & MCP Client for Mastra Code.
 * Fast, stateless JSON-RPC / REST communications with Foresight backend.
 */

const DEFAULT_FORESIGHT_URL =
  process.env.FORESIGHT_MCP_URL ?? 'http://127.0.0.1:8764'
const DEFAULT_TIMEOUT_MS = 12000

export interface ForesightClientConfig {
  baseUrl?: string
  userId?: string
  timeoutMs?: number
}

interface McpContentItem {
  type?: string
  text?: string
}

interface McpResponse {
  error?: unknown
  result?:
    | {
        content?: unknown[]
      }
    | string
    | Record<string, unknown>
}

export function parseSSEResult(body: string): Record<string, unknown> | null {
  if (!body) return null
  try {
    const parsed: unknown = JSON.parse(body)
    if (parsed && typeof parsed === 'object') {
      return parsed as Record<string, unknown>
    }
  } catch (_) {
    // Fall through to line-by-line SSE parsing
  }
  const lines = body.split('\n')
  for (const line of lines) {
    if (line.startsWith('data: ')) {
      try {
        const parsed: unknown = JSON.parse(line.slice(6))
        if (parsed && typeof parsed === 'object') {
          return parsed as Record<string, unknown>
        }
      } catch (_) {}
    }
  }
  return null
}

export async function mcpCall(
  toolName: string,
  args: Record<string, unknown>,
  config?: ForesightClientConfig,
): Promise<string | null> {
  const baseUrl = config?.baseUrl ?? DEFAULT_FORESIGHT_URL
  const timeoutMs = config?.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const mcpUrl = baseUrl.endsWith('/mcp')
    ? baseUrl
    : `${baseUrl.replace(/\/+$/, '')}/mcp`

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const resp = await fetch(mcpUrl, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
        'MCP-Protocol-Version': '2026-07-28',
        'Mcp-Method': 'tools/call',
        'Mcp-Name': toolName,
      },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: Math.floor(Math.random() * 1000000),
        method: 'tools/call',
        params: {
          name: toolName,
          arguments: args,
          _meta: {
            'io.modelcontextprotocol/protocolVersion': '2026-07-28',
            'io.modelcontextprotocol/clientCapabilities': {},
          },
        },
      }),
      signal: controller.signal,
    })

    if (!resp.ok) return null
    const text = await resp.text()
    const parsed = parseSSEResult(text) as McpResponse | null
    if (!parsed || parsed.error || !parsed.result) return null

    if (
      typeof parsed.result === 'object' &&
      parsed.result !== null &&
      'content' in parsed.result &&
      Array.isArray((parsed.result as { content?: unknown }).content)
    ) {
      const contentList = (parsed.result as { content: unknown[] }).content
      return contentList
        .filter(
          (c): c is McpContentItem & { text: string } =>
            typeof c === 'object' &&
            c !== null &&
            'type' in c &&
            (c as McpContentItem).type === 'text' &&
            'text' in c &&
            typeof (c as McpContentItem).text === 'string',
        )
        .map((c) => c.text)
        .join('\n')
    }
    return typeof parsed.result === 'string'
      ? parsed.result
      : JSON.stringify(parsed.result)
  } catch (_) {
    return null
  } finally {
    clearTimeout(timer)
  }
}

export async function fetchInjectContext(
  query: string,
  config?: ForesightClientConfig,
): Promise<string | null> {
  const baseUrl = config?.baseUrl ?? DEFAULT_FORESIGHT_URL
  const timeoutMs = config?.timeoutMs ?? DEFAULT_TIMEOUT_MS

  // 1. Try native REST inject endpoint first (ultra-fast single roundtrip)
  try {
    const injectUrl = `${baseUrl.replace(/\/+$/, '')}/ui/api/inject`
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)

    const resp = await fetch(injectUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: query }),
      signal: controller.signal,
    })
    clearTimeout(timer)

    if (resp.ok) {
      const data: unknown = await resp.json()
      if (
        data &&
        typeof data === 'object' &&
        'formatted' in data &&
        typeof (data as { formatted?: unknown }).formatted === 'string'
      ) {
        const formatted = (data as { formatted: string }).formatted.trim()
        if (formatted) {
          return formatted
        }
      }
    }
  } catch (_) {}

  // 2. Fallback to MCP tools/call inject_context
  return await mcpCall(
    'inject_context',
    {
      conversation_text: query,
      max_memories: 6,
      min_relevance: 0.01,
      user_id: config?.userId ?? 'default',
    },
    config,
  )
}

export async function autoCaptureTurn(
  sessionId: string,
  userMessage: string,
  assistantMessage: string,
  config?: ForesightClientConfig,
): Promise<void> {
  if (!userMessage || userMessage.trim().length < 4) return

  const messages: Array<{ role: string; content: string }> = [
    { role: 'user', content: userMessage.slice(0, 4000) },
  ]
  if (assistantMessage) {
    messages.push({
      role: 'assistant',
      content: assistantMessage.slice(0, 2000),
    })
  }

  // Fire-and-forget background capture
  void mcpCall(
    'process_session_transcript',
    {
      session_id: sessionId || 'mastracode-session',
      messages,
      user_id: config?.userId ?? 'default',
    },
    config,
  ).catch(() => {})
}
