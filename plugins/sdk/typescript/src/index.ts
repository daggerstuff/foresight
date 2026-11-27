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
  sessionFallback?: string
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

/** First value that is set and not blank; a blank variable is not a setting. */
function firstNonBlank(
  ...values: Array<string | undefined>
): string | undefined {
  for (const value of values) {
    if (value !== undefined && value.trim() !== '') return value
  }
  return undefined
}

export function resolveBaseUrl(
  env: Record<string, string | undefined> = process.env,
): string {
  return (
    firstNonBlank(env.FORESIGHT_MCP_URL, env.FORESIGHT_URL) ??
    FORESIGHT_DEFAULT_URL
  )
}

export function defaultCredentialsPath(
  env: Record<string, string | undefined> = process.env,
  homeDir?: string,
): string {
  const configHome =
    firstNonBlank(env.XDG_CONFIG_HOME) ?? `${homeDir ?? env.HOME ?? '~'}/.config`
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
  const identity = config?.identity || pick('FORESIGHT_IDENTITY')
  const identityLane = identity ? identity.split('@', 1)[0] : ''
  const userId =
    firstNonBlank(config?.userId, pick('FORESIGHT_USER_ID')) ?? ''
  return {
    baseUrl:
      firstNonBlank(
        config?.baseUrl,
        env.FORESIGHT_MCP_URL,
        env.FORESIGHT_URL,
        file.FORESIGHT_MCP_URL,
        file.FORESIGHT_URL,
      ) ?? FORESIGHT_DEFAULT_URL,
    apiKey: config?.apiKey ?? pick('FORESIGHT_API_KEY'),
    identity,
    userId,
    // A user id — explicit or from the environment — outranks the lane derived
    // from the identity: the same identity can switch lanes, and authHeaders/
    // resolvedLane would otherwise send X-User-Id for the identity's own lane.
    lane: firstNonBlank(userId, identityLane) ?? '',
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

export function restBaseUrl(credentials: ForesightCredentials): string {
  const url = splitBaseUrl(credentials.baseUrl)
  const path = url.pathname.replace(/\/+$/, '')
  return withPath(url, path.endsWith('/mcp') ? path.slice(0, -'/mcp'.length) : path)
}

/**
 * Split a configured base URL, refusing anything that is not http(s).
 *
 * Paths are rebuilt from the parsed URL rather than concatenated onto the
 * string, because appending to a base that carries a query lands inside the
 * query (`?route=blue/mcp`) and never reaches the endpoint.
 */
function splitBaseUrl(baseUrl: string): URL {
  let parsed: URL
  try {
    parsed = new URL(baseUrl)
  } catch {
    throw new ForesightError(`invalid Foresight URL: ${baseUrl}`)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new ForesightError(
      `invalid Foresight URL: ${baseUrl} (expected http: or https:)`,
    )
  }
  return parsed
}

/** Rebuild `url` with `path`, keeping its query and dropping its fragment. */
function withPath(url: URL, path: string): string {
  const built = new URL(url.toString())
  built.pathname = path
  built.hash = ''
  return built.toString()
}

/** MCP endpoint for a base URL, with `/mcp` placed in the path. */
function mcpUrlFor(credentials: ForesightCredentials): string {
  const url = splitBaseUrl(credentials.baseUrl)
  const path = url.pathname.replace(/\/+$/, '')
  return withPath(url, path.endsWith('/mcp') ? path : `${path}/mcp`)
}

/** REST endpoint for a base URL, with any terminal `/mcp` removed. */
function restUrlFor(credentials: ForesightCredentials, path: string): string {
  const url = splitBaseUrl(credentials.baseUrl)
  const basePath = url.pathname.replace(/\/+$/, '')
  const restPath = basePath.endsWith('/mcp')
    ? basePath.slice(0, -'/mcp'.length)
    : basePath
  return withPath(url, `${restPath}${path}`)
}

function isLoopbackHost(hostname: string): boolean {
  const host = hostname.replace(/^\[|\]$/g, '').toLowerCase()
  if (!host) return false
  if (host === 'localhost' || host.endsWith('.localhost')) return true
  return /^127(?:\.\d{1,3}){3}$/.test(host) || host === '::1'
}

/**
 * Refuse to put credentials on the wire in cleartext off loopback: a
 * configured `http://` base URL would otherwise expose the API key to any
 * network observer. Loopback development stays supported.
 */
export function assertCredentialTransport(
  url: string,
  hasCredentials: boolean,
): void {
  if (!hasCredentials) return
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    throw new ForesightError(`invalid Foresight URL: ${url}`)
  }
  if (parsed.protocol === 'https:') return
  if (parsed.protocol === 'http:' && isLoopbackHost(parsed.hostname)) return
  throw new ForesightError(
    `refusing to send credentials over cleartext HTTP to ${parsed.hostname}; ` +
      'use https:// or a loopback address',
  )
}

/**
 * The lane the REST path selects, so the MCP fallbacks read the same lane.
 *
 * Empty when neither an identity nor a user id is configured: an invented
 * placeholder would select a lane the API key cannot see, so callers omit the
 * argument and let the server use the key's own lane.
 */
export function resolvedLane(credentials: ForesightCredentials): string {
  return credentials.lane || credentials.userId
}

/** The MCP `user_id` argument, omitted when no lane is configured. */
export function laneArgs(
  credentials: ForesightCredentials,
): Record<string, unknown> {
  const lane = resolvedLane(credentials)
  return lane ? { user_id: lane } : {}
}

function carriesAuthorization(headers?: Record<string, string>): boolean {
  return Object.keys(headers ?? {}).some(
    (name) => name.toLowerCase() === 'authorization',
  )
}

function isNotificationFrame(frame: Record<string, unknown>): boolean {
  return 'method' in frame && !('id' in frame)
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
  let fallback: Record<string, unknown> | null = null
  for (const event of iterSSEEvents(body)) {
    let parsed: unknown
    try {
      parsed = JSON.parse(event)
    } catch {
      continue
    }
    if (!parsed || typeof parsed !== 'object') continue
    const frame = parsed as Record<string, unknown>
    if ('result' in frame || 'error' in frame) return frame
    if (fallback === null && !isNotificationFrame(frame)) fallback = frame
  }
  return fallback
}

/**
 * Yield each SSE event's data payload, joined across that event's lines.
 *
 * SSE ends an event on a blank line and allows one payload to span several
 * `data:` lines, so decoding each line on its own drops a split payload. A
 * single leading space after the colon is framing, not payload, and `:`
 * comment lines carry no data.
 */
function* iterSSEEvents(body: string): Generator<string> {
  let data: string[] = []
  for (const rawLine of body.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (!line) {
      if (data.length) {
        yield data.join('\n')
        data = []
      }
      continue
    }
    if (line.startsWith(':')) continue
    if (line.startsWith('data:')) {
      const value = line.slice('data:'.length)
      data.push(value.startsWith(' ') ? value.slice(1) : value)
    }
  }
  if (data.length) yield data.join('\n')
}

export interface PostJsonResult {
  status: number
  body: string
}

/**
 * POST a JSON payload with a timeout. Resolves `{status, body}` for a
 * non-redirect HTTP status; rejects on transport failure, a redirect status,
 * or an invalid/cleartext URL (the caller decides fail-safe policy).
 */
export async function postJson(
  url: string,
  payload: unknown,
  options?: {
    headers?: Record<string, string>
    timeoutMs?: number
  },
): Promise<PostJsonResult> {
  assertCredentialTransport(url, carriesAuthorization(options?.headers))
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
      // `fetch` follows a redirect by default, which would repeat the POST body
      // against whatever host the Location header names. Refuse instead.
      redirect: 'error',
      signal: controller.signal,
    })
    if (resp.status >= 300 && resp.status < 400) {
      throw new ForesightError(
        `refusing redirect ${resp.status} from ${url}: the endpoint moved`,
      )
    }
    return { status: resp.status, body: await resp.text() }
  } catch (err) {
    throw new ForesightError(
      err instanceof Error ? err.message : 'Foresight transport failure',
    )
  } finally {
    clearTimeout(timer)
  }
}

export async function mcpCall(
  toolName: string,
  args: Record<string, unknown>,
  config?: ForesightClientConfig,
  resolvedCredentials?: ForesightCredentials,
): Promise<string | null> {
  const credentials = resolvedCredentials ?? (await loadCredentials(config))
  const timeoutMs = config?.timeoutMs ?? DEFAULT_TIMEOUT_MS

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  try {
    // Built inside the try: an unusable base URL is a transport failure like
    // any other, and this function's contract is to answer null, not throw.
    const mcpUrl = mcpUrlFor(credentials)
    assertCredentialTransport(mcpUrl, Boolean(credentials.apiKey))
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
          arguments: { params: args },
          _meta: {
            'io.modelcontextprotocol/protocolVersion':
              FORESIGHT_MCP_PROTOCOL_VERSION,
            'io.modelcontextprotocol/clientCapabilities': {},
            ...(credentials.apiKey ? { api_key: credentials.apiKey } : {}),
          },
        },
      }),
      redirect: 'error',
      signal: controller.signal,
    })

    if (!resp.ok) return null
    const text = await resp.text()
    const parsed = parseSSEResult(text) as McpResponse | null
    if (!parsed || parsed.error || !parsed.result) return null
    if (
      typeof parsed.result === 'object' &&
      (parsed.result as { isError?: unknown }).isError
    ) {
      return null
    }

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
  resolvedCredentials?: ForesightCredentials,
): Promise<{ status: number; data: unknown } | null> {
  try {
    const credentials = resolvedCredentials ?? (await loadCredentials(config))
    const { status, body } = await postJson(
      restUrlFor(credentials, path),
      payload,
      {
        headers: authHeaders(credentials),
        timeoutMs: config?.timeoutMs ?? timeoutMs ?? DEFAULT_TIMEOUT_MS,
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
  // One credential load for both attempts: each load re-reads credentials.env,
  // and the fallback must read the identical lane the REST call used.
  const credentials = await loadCredentials(config)
  // 1. Native REST inject endpoint first (ultra-fast single roundtrip)
  const rest = await postPath(
    '/ui/api/inject',
    { text: query },
    config,
    undefined,
    credentials,
  )
  if (rest && rest.status >= 200 && rest.status < 300) {
    const data = rest.data as { formatted?: unknown } | null
    if (data && typeof data.formatted === 'string') {
      const formatted = data.formatted.trim()
      if (formatted) return formatted
    }
  }

  // 2. Fallback to MCP tools/call inject_context
  return await mcpCall(
    'inject_context',
    {
      conversation_text: query,
      max_memories: 6,
      min_relevance: 0.01,
      ...laneArgs(credentials),
    },
    config,
    credentials,
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

export async function postTelemetry(
  surface: string,
  payload: Record<string, unknown>,
  config?: ForesightClientConfig,
): Promise<boolean> {
  const result = await postPath(
    '/ui/api/telemetry/gating',
    // The surface names this call site, so a payload cannot relabel it.
    { ...payload, surface },
    config,
    2000,
  )
  return result !== null && result.status >= 200 && result.status < 300
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

  const credentials = await loadCredentials(config)
  // Fire-and-forget background capture via MCP distillation
  void mcpCall(
    'process_session_transcript',
    {
      session_id: sessionId || config?.sessionFallback || 'agent-session',
      messages,
      ...laneArgs(credentials),
    },
    config,
  ).catch(() => {})
}
