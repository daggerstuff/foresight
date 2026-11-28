import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  autoCaptureTurn,
  authHeaders,
  captureText,
  captureTranscript,
  defaultCredentialsPath,
  fetchInjectContext,
  loadCredentials,
  mcpCall,
  parseEnvFile,
  parseSSEResult,
  postJson,
  postTelemetry,
  resolveBaseUrl,
  trackEvent,
} from '../src/index.js'

const ENV_KEYS = [
  'FORESIGHT_MCP_URL',
  'FORESIGHT_URL',
  'FORESIGHT_API_KEY',
  'FORESIGHT_IDENTITY',
  'FORESIGHT_USER_ID',
  'XDG_CONFIG_HOME',
] as const

function stubFetch(impl: (url: string, init?: RequestInit) => unknown) {
  const spy = vi.fn(async (url: unknown, init?: RequestInit) => {
    const result = impl(String(url), init)
    if (result instanceof Response) return result
    return new Response(JSON.stringify(result ?? {}), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', spy)
  return spy
}

afterEach(() => {
  vi.unstubAllGlobals()
  for (const key of ENV_KEYS) delete process.env[key]
})

describe('env + credentials', () => {
  it('resolveBaseUrl prefers MCP_URL then URL then default', () => {
    expect(resolveBaseUrl({})).toBe('http://127.0.0.1:8764')
    expect(resolveBaseUrl({ FORESIGHT_URL: 'http://x' })).toBe('http://x')
    expect(
      resolveBaseUrl({ FORESIGHT_URL: 'http://x', FORESIGHT_MCP_URL: 'http://y' }),
    ).toBe('http://y')
  })

  it('defaultCredentialsPath honors XDG_CONFIG_HOME', () => {
    expect(defaultCredentialsPath({ HOME: '/home/u' })).toBe(
      '/home/u/.config/foresight/credentials.env',
    )
    expect(
      defaultCredentialsPath({ HOME: '/home/u', XDG_CONFIG_HOME: '/xdg' }),
    ).toBe('/xdg/foresight/credentials.env')
  })

  it('parseEnvFile handles comments, quotes, blanks', () => {
    const parsed = parseEnvFile(
      '# comment\nFOO=bar\nQUOTED="baz qux"\nEMPTY=\nSINGLE=\'v\'\n',
    )
    expect(parsed).toEqual({ FOO: 'bar', QUOTED: 'baz qux', EMPTY: '', SINGLE: 'v' })
  })

  it('loadCredentials resolves env and derives lane from identity', async () => {
    process.env.FORESIGHT_IDENTITY = 'alice@acme'
    process.env.FORESIGHT_API_KEY = 'secret'
    const creds = await loadCredentials({
      credentialsPath: '/nonexistent/credentials.env',
    })
    expect(creds.apiKey).toBe('secret')
    expect(creds.identity).toBe('alice@acme')
    expect(creds.lane).toBe('alice')
    expect(creds.baseUrl).toBe('http://127.0.0.1:8764')
  })

  it('authHeaders emits Bearer + X-User-Id only when set', () => {
    expect(
      authHeaders({
        baseUrl: 'http://x',
        apiKey: 'k',
        identity: 'bob@org',
        userId: '',
        lane: 'bob',
      }),
    ).toEqual({
      'Content-Type': 'application/json',
      Authorization: 'Bearer k',
      'X-User-Id': 'bob',
    })
    expect(
      authHeaders({ baseUrl: 'http://x', apiKey: '', identity: '', userId: '', lane: '' }),
    ).toEqual({ 'Content-Type': 'application/json' })
  })
})

describe('transport', () => {
  it('postJson returns status and body', async () => {
    stubFetch(() => ({ ok: true }))
    const result = await postJson('http://x/api', { a: 1 })
    expect(result.status).toBe(200)
  })

  it('postJson rejects on transport failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('ECONNREFUSED')
      }),
    )
    await expect(postJson('http://x', {})).rejects.toThrow()
  })

  it('parseSSEResult handles plain JSON and data: frames', () => {
    expect(parseSSEResult('{"result": {"content": []}}')).toEqual({
      result: { content: [] },
    })
    expect(parseSSEResult('data: {"a":1}\n\ndata: {"b":2}\n')).toEqual({ a: 1 })
    expect(parseSSEResult('not json')).toBeNull()
    expect(parseSSEResult('')).toBeNull()
  })
})

describe('client helpers (fail-safe)', () => {
  it('fetchInjectContext returns formatted from REST inject', async () => {
    const spy = stubFetch((url) => {
      expect(url).toBe('http://127.0.0.1:8764/ui/api/inject')
      return { formatted: '  memories here  ' }
    })
    const out = await fetchInjectContext('hello', {
      credentialsPath: '/nonexistent',
    })
    expect(out).toBe('memories here')
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('fetchInjectContext falls back to MCP when REST fails', async () => {
    stubFetch((url) => {
      if (url.endsWith('/ui/api/inject')) throw new Error('down')
      return {
        result: { content: [{ type: 'text', text: 'mcp memories' }] },
      }
    })
    const out = await fetchInjectContext('hello', {
      credentialsPath: '/nonexistent',
    })
    expect(out).toBe('mcp memories')
  })

  it('fetchInjectContext returns null when everything fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('down')
      }),
    )
    expect(
      await fetchInjectContext('hello', { credentialsPath: '/nonexistent' }),
    ).toBeNull()
  })

  it('mcpCall extracts text content', async () => {
    stubFetch(() => ({
      result: { content: [{ type: 'text', text: 'tool output' }] },
    }))
    const out = await mcpCall('search_memories', { query: 'x' }, {
      credentialsPath: '/nonexistent',
    })
    expect(out).toBe('tool output')
  })

  it('mcpCall returns null on JSON-RPC error', async () => {
    stubFetch(() => ({ error: { code: -1 } }))
    expect(
      await mcpCall('x', {}, { credentialsPath: '/nonexistent' }),
    ).toBeNull()
  })

  it('captureText posts to /ui/api/capture', async () => {
    const spy = stubFetch(() => ({}))
    const ok = await captureText('sess', 'hello', undefined, {
      credentialsPath: '/nonexistent',
    })
    expect(ok).toBe(true)
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('http://127.0.0.1:8764/ui/api/capture')
    expect(JSON.parse(String(init.body))).toMatchObject({
      text: 'hello',
      session_id: 'sess',
    })
  })

  it('captureTranscript posts to /session/transcript', async () => {
    const spy = stubFetch(() => ({}))
    const ok = await captureTranscript(
      'sess',
      [{ role: 'user', content: 'hi' }],
      { credentialsPath: '/nonexistent' },
    )
    expect(ok).toBe(true)
    expect(spy.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8764/session/transcript',
    )
  })

  it('postTelemetry posts to gating endpoint', async () => {
    const spy = stubFetch(() => ({}))
    await postTelemetry('test_surface', { decision: 'injected' }, {
      credentialsPath: '/nonexistent',
    })
    expect(spy.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8764/ui/api/telemetry/gating',
    )
  })

  it('trackEvent posts schema-shaped payload and returns true', async () => {
    const spy = stubFetch(() => ({}))
    const ok = await trackEvent(
      'recall.injected',
      {
        plugin: 'mastracode',
        surface: 'extension',
        latencyMs: 12.5,
        fields: { memories_injected: 2 },
      },
      { credentialsPath: '/nonexistent' },
    )
    expect(ok).toBe(true)
    const [url, init] = spy.mock.calls[0] as unknown as [string, RequestInit]
    expect(url).toBe('http://127.0.0.1:8764/ui/api/telemetry/event')
    expect(JSON.parse(String(init.body))).toEqual({
      event: 'recall.injected',
      plugin: 'mastracode',
      surface: 'extension',
      latency_ms: 12.5,
      fields: { memories_injected: 2 },
    })
  })

  it('trackEvent returns false when server is down', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('down')
      }),
    )
    expect(
      await trackEvent('x.y', undefined, { credentialsPath: '/nonexistent' }),
    ).toBe(false)
  })

  it('autoCaptureTurn skips short user messages', async () => {
    const spy = stubFetch(() => ({}))
    await autoCaptureTurn('s', 'hi', 'there', {
      credentialsPath: '/nonexistent',
    })
    expect(spy).not.toHaveBeenCalled()
  })
})
