/**
 * @foresight/plugin-sdk — shared client for Foresight agent plugins (FOR-69).
 *
 * Every helper is fail-safe: transport/HTTP failures resolve to `null`/`false`
 * (or throw `ForesightError` only when `throwOnError` is set) so a down
 * Foresight server can never break the host agent session.
 */

export const FORESIGHT_DEFAULT_URL = 'http://127.0.0.1:8764'
export const FORESIGHT_MCP_PROTOCOL_VERSION = '2026-07-28'
export const DEFAULT_TIMEOUT_MS = 12000

export class ForesightError extends Error {}

export interface ForesightCredentials {
  baseUrl: string
  apiKey: string
  identity: string
  userId: string
  /** Memory lane derived from identity (`alice@acme` -> `alice`). */
  lane: string
}

export interface ForesightClientConfig {
  baseUrl?: string
  apiKey?: string
  identity?: string
  userId?: string
  timeoutMs?: number
  /** Override credentials.env location (default ~/.config/foresight/credentials.env). */
  credentialsPath?: string
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

export function resolveBaseUrl(
  env: Record<string, string | undefined> = process.env,
): string {
  return (
    env.FORESIGHT_MCP_URL ??
    env.FORESIGHT_URL ??
    FORESIGHT_DEFAULT_URL
  )
}

export function defaultCredentialsPath(
  env: Record<string, string | undefined> = process.env,
  homeDir?: string,
): string {
  const configHome =
    env.XDG_CONFIG_HOME ?? `${homeDir ?? env.HOME ?? '~'}/.config`
  return `${configHome}/foresight/credentials.env`
}

/** Parse KEY=VALUE env-file contents (comments, blank lines, quotes ok). */
export function parseEnvFile(contents: string): Record<string, string> {
  const values: Record<string, string> = {}
  for (const rawLine of contents.split('\n')) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const eq = line.indexOf('=')
    const key = line.slice(0, eq).trim()
    const value = line
      .slice(eq + 1)
      .trim()
      .replace(/^["']|["']$/g, '')
    values[key] = value
  }
  return values
}

async function readEnvFile(path: string): Promise<Record<string, string>> {
  try {
    const { readFile } = await import('node:fs/promises')
    return parseEnvFile(await readFile(path, 'utf8'))
  } catch {
    return {}
  }
}

/**
 * Resolve credentials: explicit config > environment > credentials.env.
 * Never throws — missing credentials yield empty strings.
 */
export async function loadCredentials(
  config?: ForesightClientConfig,
): Promise<ForesightCredentials> {
  const env = process.env
  const file = await readEnvFile(
    config?.credentialsPath ?? defaultCredentialsPath(env),
  )
  const pick = (...names: string[]): string => {
    for (const name of names) {
      const value = env[name] || file[name]
      if (value) return value
    }
    return ''
  }
  const identity = pick('FORESIGHT_IDENTITY')
  const lane = identity ? identity.split('@', 1)[0] : ''
  return {
    baseUrl:
      config?.baseUrl ??
      env.FORESIGHT_MCP_URL ??
      env.FORESIGHT_URL ??
      file.FORESIGHT_MCP_URL ??
      file.FORESIGHT_URL ??
      FORESIGHT_DEFAULT_URL,
    apiKey: config?.apiKey ?? pick('FORESIGHT_API_KEY'),
    identity,
    userId: config?.userId ?? pick('FORESIGHT_USER_ID'),
    lane: lane || (config?.userId ?? pick('FORESIGHT_USER_ID')),
  }
}

export function authHeaders(
  credentials: ForesightCredentials,
): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (credentials.apiKey) {
    headers.Authorization = `Bearer ${credentials.apiKey}`
  }
  if (credentials.lane) {
    headers['X-User-Id'] = credentials.lane
  }
  return headers
}

export function parseSSEResult(body: string): Record<string, unknown> | null {
  if (!body) return null
  try {
    const parsed: unknown = JSON.parse(body)
    if (parsed && typeof parsed === 'object') {
      return parsed as Record<string, unknown>
    }
  } catch {
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
      } catch {
        // keep scanning
      }
    }
  }
  return null
}

export interface PostJsonResult {
  status: number
  body: string
}

/**
 * POST a JSON payload with a timeout. Resolves `{status, body}` for any HTTP
 * status; rejects only on transport failure (caller decides fail-safe policy).
 */
export async function postJson(
  url: string,
  payload: unknown,
  options?: {
    headers?: Record<string, string>
    timeoutMs?: number
  },
): Promise<PostJsonResult> {
  const controller = new AbortController()
  const timer = setTimeout(
    () => controller.abort(),
    options?.timeoutMs ?? DEFAULT_TIMEOUT_MS,
  )
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: options?.headers ?? { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    return { status: resp.status, body: await resp.text() }
  } finally {
    clearTimeout(timer)
  }
}

export async function mcpCall(
  toolName: string,
  args: Record<string, unknown>,
  config?: ForesightClientConfig,
): Promise<string | null> {
  const credentials = await loadCredentials(config)
  const baseUrl = credentials.baseUrl.replace(/\/+$/, '')
  const mcpUrl = baseUrl.endsWith('/mcp') ? baseUrl : `${baseUrl}/mcp`
  const timeoutMs = config?.timeoutMs ?? DEFAULT_TIMEOUT_MS

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const resp = await fetch(mcpUrl, {
      method: 'POST',
      headers: {
        ...authHeaders(credentials),
        Accept: 'application/json, text/event-stream',
        'MCP-Protocol-Version': FORESIGHT_MCP_PROTOCOL_VERSION,
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
            'io.modelcontextprotocol/protocolVersion':
              FORESIGHT_MCP_PROTOCOL_VERSION,
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
  } catch {
    return null
  } finally {
    clearTimeout(timer)
  }
}

async function postPath(
  path: string,
  payload: unknown,
  config?: ForesightClientConfig,
  timeoutMs?: number,
): Promise<{ status: number; data: unknown } | null> {
  try {
    const credentials = await loadCredentials(config)
    const { status, body } = await postJson(
      `${credentials.baseUrl.replace(/\/+$/, '')}${path}`,
      payload,
      {
        headers: authHeaders(credentials),
        timeoutMs: timeoutMs ?? config?.timeoutMs ?? DEFAULT_TIMEOUT_MS,
      },
    )
    let data: unknown = null
    try {
      data = JSON.parse(body)
    } catch {
      data = body
    }
    return { status, data }
  } catch {
    return null
  }
}

export async function fetchInjectContext(
  query: string,
  config?: ForesightClientConfig,
): Promise<string | null> {
  // 1. Native REST inject endpoint first (ultra-fast single roundtrip)
  const rest = await postPath('/ui/api/inject', { text: query }, config)
  if (rest && rest.status >= 200 && rest.status < 300) {
    const data = rest.data as { formatted?: unknown } | null
    if (data && typeof data.formatted === 'string') {
      const formatted = data.formatted.trim()
      if (formatted) return formatted
    }
  }

  // 2. Fallback to MCP tools/call inject_context
  const credentials = await loadCredentials(config)
  return await mcpCall(
    'inject_context',
    {
      conversation_text: query,
      max_memories: 6,
      min_relevance: 0.01,
      user_id: credentials.userId || 'default',
    },
    config,
  )
}

export async function captureText(
  sessionId: string,
  text: string,
  messages?: Array<{ role: string; content: string }>,
  config?: ForesightClientConfig,
): Promise<boolean> {
  const payload: Record<string, unknown> = {
    text,
    session_id: sessionId,
  }
  if (messages && messages.length > 0) payload.messages = messages
  const result = await postPath('/ui/api/capture', payload, config, 5000)
  return result !== null && result.status >= 200 && result.status < 300
}

export async function captureTranscript(
  sessionId: string,
  messages: Array<{ role: string; content: string }>,
  config?: ForesightClientConfig,
): Promise<boolean> {
  const result = await postPath(
    '/session/transcript',
    { session_id: sessionId, messages },
    config,
    12000,
  )
  return result !== null && result.status >= 200 && result.status < 300
}

export interface PluginEventOptions {
  plugin?: string
  surface?: string
  latencyMs?: number
  /** Scalar-only fields (counts, scores, booleans) — never memory content. */
  fields?: Record<string, string | number | boolean | null>
}

/**
 * Record a plugin benefit/health event via `/ui/api/telemetry/event` (FOR-70).
 * Fail-safe: resolves `false` on any failure. Event catalog + privacy rules:
 * `plugins/telemetry/README.md`.
 */
export async function trackEvent(
  event: string,
  options?: PluginEventOptions,
  config?: ForesightClientConfig,
): Promise<boolean> {
  const payload: Record<string, unknown> = {
    event,
    plugin: options?.plugin ?? 'unknown',
    surface: options?.surface ?? 'unknown',
  }
  if (options?.latencyMs !== undefined) payload.latency_ms = options.latencyMs
  if (options?.fields) payload.fields = options.fields
  const result = await postPath('/ui/api/telemetry/event', payload, config, 2000)
  return result !== null && result.status >= 200 && result.status < 300
}

export async function postTelemetry(
  surface: string,
  payload: Record<string, unknown>,
  config?: ForesightClientConfig,
): Promise<void> {
  await postPath(
    '/ui/api/telemetry/gating',
    { surface, ...payload },
    config,
    2000,
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

  // Fire-and-forget background capture via MCP distillation
  void mcpCall(
    'process_session_transcript',
    {
      session_id: sessionId || 'agent-session',
      messages,
      user_id: (await loadCredentials(config)).userId || 'default',
    },
    config,
  ).catch(() => {})
}
