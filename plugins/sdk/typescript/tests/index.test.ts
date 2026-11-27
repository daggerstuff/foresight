import { afterEach, describe, expect, it, vi } from 'vitest'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import {
  autoCaptureTurn,
  authHeaders,
  captureText,
  captureTranscript,
  defaultCredentialsPath,
  fetchInjectContext,
  laneArgs,
  loadCredentials,
  mcpCall,
  parseEnvFile,
  parseSSEResult,
  postJson,
  postTelemetry,
  resolveBaseUrl,
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

  it('loadCredentials derives the lane from the credentials file', async () => {
    const dir = mkdtempSync(join(tmpdir(), 'foresight-sdk-'))
    const path = join(dir, 'credentials.env')
    writeFileSync(
      path,
      'FORESIGHT_IDENTITY=dana@acme\nFORESIGHT_API_KEY=file-key\n',
    )
    const creds = await loadCredentials({ credentialsPath: path })
    expect(creds.apiKey).toBe('file-key')
    // The plugin's own resolver read config and process.env but never this
    // file, so tools and capture travelled in different lanes.
    expect(creds.lane).toBe('dana')
    expect(laneArgs(creds)).toEqual({ user_id: 'dana' })
  })

  it('loadCredentials lets an explicit user id outrank the identity lane', async () => {
    process.env.FORESIGHT_IDENTITY = 'alice@acme'
    const creds = await loadCredentials({
      credentialsPath: '/nonexistent/credentials.env',
      userId: 'bob',
    })
    expect(creds.identity).toBe('alice@acme')
    expect(creds.lane).toBe('bob')
    expect(laneArgs(creds)).toEqual({ user_id: 'bob' })
  })

  it('laneArgs omits the argument instead of inventing a lane', () => {
    expect(
      laneArgs({ baseUrl: '', apiKey: '', identity: '', userId: '', lane: '' }),
    ).toEqual({})
    expect(
      laneArgs({ baseUrl: '', apiKey: '', identity: '', userId: 'bob', lane: '' }),
    ).toEqual({ user_id: 'bob' })
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

  it('mcpCall treats a tool result marked isError as a failure', async () => {
    stubFetch(() => ({
      result: {
        content: [{ type: 'text', text: 'context block is read-only' }],
        isError: true,
      },
    }))
    expect(
      await mcpCall(
        'update_context_block',
        { name: 'coding_guidelines' },
        { credentialsPath: '/nonexistent' },
      ),
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
    const accepted = await postTelemetry(
      'test_surface',
      { decision: 'injected' },
      { credentialsPath: '/nonexistent' },
    )
    expect(accepted).toBe(true)
    expect(spy.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8764/ui/api/telemetry/gating',
    )
  })

  it('autoCaptureTurn skips short user messages', async () => {
    const spy = stubFetch(() => ({}))
    await autoCaptureTurn('s', 'hi', 'there', {
      credentialsPath: '/nonexistent',
    })
    expect(spy).not.toHaveBeenCalled()
  })
})

describe('REST payload and fail-safe contract', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('captureTranscript posts session_id and messages', async () => {
    const sdk = await import('../src/index.js')
    const sent: Array<{ url: string; body: unknown }> = []
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      sent.push({ url: String(url), body: JSON.parse(String(init?.body)) })
      return new Response('{}', { status: 200 })
    })
    await expect(
      sdk.captureTranscript('sess-1', [{ role: 'user', content: 'hi' }]),
    ).resolves.toBe(true)
    expect(sent[0]?.body).toEqual({
      session_id: 'sess-1',
      messages: [{ role: 'user', content: 'hi' }],
    })
  })

  it('captureText and captureTranscript return false on 5xx', async () => {
    const sdk = await import('../src/index.js')
    vi.stubGlobal('fetch', async () => new Response('boom', { status: 500 }))
    await expect(sdk.captureText('s', 'hello')).resolves.toBe(false)
    await expect(
      sdk.captureTranscript('s', [{ role: 'user', content: 'hi' }]),
    ).resolves.toBe(false)
  })

  it('postTelemetry reports a rejected post as false without throwing', async () => {
    const sdk = await import('../src/index.js')
    vi.stubGlobal('fetch', async () => new Response('nope', { status: 404 }))
    await expect(
      sdk.postTelemetry('claude_hook', { surface: 'claude_hook' }),
    ).resolves.toBe(false)
  })

  it('postJson refuses cleartext credentials off loopback', async () => {
    const spy = stubFetch(() => new Response('{}', { status: 200 }))
    await expect(
      postJson(
        'http://foresight.example.com/mcp',
        {},
        { headers: { Authorization: 'Bearer k' } },
      ),
    ).rejects.toThrow(/cleartext/)
    expect(spy).not.toHaveBeenCalled()
  })

  it('postJson still sends credentials to loopback over http', async () => {
    const spy = stubFetch(() => new Response('{}', { status: 200 }))
    await expect(
      postJson(
        'http://127.0.0.1:8764/mcp',
        {},
        { headers: { Authorization: 'Bearer k' } },
      ),
    ).resolves.toEqual({ status: 200, body: '{}' })
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('refuses redirects on both transports so a body credential cannot move', async () => {
    const inits: Array<RequestInit | undefined> = []
    vi.stubGlobal('fetch', async (_url: string, init?: RequestInit) => {
      inits.push(init)
      return new Response('{}', { status: 200 })
    })
    await postJson('http://127.0.0.1:8764/ui/api/capture', {})
    await mcpCall(
      'search_memories',
      { query: 'x' },
      { credentialsPath: '/nonexistent' },
    )
    expect(inits).toHaveLength(2)
    expect(inits[0]?.redirect).toBe('error')
    expect(inits[1]?.redirect).toBe('error')
  })

  it('mcpCall refuses to send the api key over cleartext http', async () => {
    const spy = stubFetch(() => new Response('{}', { status: 200 }))
    await expect(
      mcpCall(
        'search_memories',
        { query: 'x' },
        { baseUrl: 'http://foresight.example.com', apiKey: 'secret' },
      ),
    ).resolves.toBeNull()
    expect(spy).not.toHaveBeenCalled()
  })

  it('parseSSEResult skips notification frames before the response', () => {
    const body = [
      'data: {"jsonrpc":"2.0","method":"notifications/message","params":{"level":"info"}}',
      '',
      'data: {"jsonrpc":"2.0","id":7,"result":{"content":[{"type":"text","text":"hi"}]}}',
      '',
    ].join('\n')
    expect(parseSSEResult(body)).toEqual({
      jsonrpc: '2.0',
      id: 7,
      result: { content: [{ type: 'text', text: 'hi' }] },
    })
  })

  it('fetchInjectContext falls back to MCP and returns null when both fail', async () => {
    const sdk = await import('../src/index.js')
    const urls: string[] = []
    vi.stubGlobal('fetch', async (url: string) => {
      urls.push(String(url))
      return new Response('down', { status: 503 })
    })
    await expect(sdk.fetchInjectContext('q')).resolves.toBeNull()
    expect(urls.some((url) => url.endsWith('/mcp'))).toBe(true)
  })

  it('resolvedLane resolves identity lane, then user id, then nothing', async () => {
    const sdk = await import('../src/index.js')
    const base = { baseUrl: '', apiKey: '', identity: '', userId: '', lane: '' }
    expect(
      sdk.resolvedLane({ ...base, identity: 'alice@acme', lane: 'alice' }),
    ).toBe('alice')
    expect(sdk.resolvedLane({ ...base, userId: 'bob' })).toBe('bob')
    // An explicit lane wins over a user id it may have been derived from.
    expect(sdk.resolvedLane({ ...base, lane: 'alice', userId: 'bob' })).toBe(
      'alice',
    )
    // No placeholder: an invented lane selects a store the API key cannot see,
    // so callers omit the argument and the server uses the key's own lane.
    expect(sdk.resolvedLane(base)).toBe('')
  })

  it('fetchInjectContext sends the resolved lane in the MCP fallback', async () => {
    const bodies: string[] = []
    vi.stubGlobal(
      'fetch',
      async (url: string, init?: RequestInit) => {
        if (String(url).endsWith('/ui/api/inject')) {
          return new Response('down', { status: 503 })
        }
        bodies.push(String(init?.body ?? ''))
        return new Response(
          JSON.stringify({
            result: { content: [{ type: 'text', text: 'mcp memories' }] },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        )
      },
    )
    const out = await fetchInjectContext('hello', {
      credentialsPath: '/nonexistent',
      identity: 'alice@acme',
    })
    expect(out).toBe('mcp memories')
    expect(bodies).toHaveLength(1)
    expect(bodies[0]).toContain('"user_id":"alice"')
  })

  it(
    'captureText honors a configured timeoutMs over the 5000 ms default',
    async () => {
      const delays: unknown[] = []
      const realSetTimeout = globalThis.setTimeout
      const timerSpy = vi.spyOn(globalThis, 'setTimeout')
      timerSpy.mockImplementation(((cb: () => void, ms?: number) => {
        delays.push(ms)
        return realSetTimeout(cb, ms)
      }) as never)
      const fetchMock = vi.fn(
        (_url: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => {
              reject(new Error('aborted'))
            })
          }),
      )
      vi.stubGlobal('fetch', fetchMock)
      try {
        await expect(
          captureText('session-1', 'hello', undefined, {
            timeoutMs: 40,
            credentialsPath: '/nonexistent',
          }),
        ).resolves.toBe(false)
      } finally {
        timerSpy.mockRestore()
      }
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(delays).toContain(40)
      expect(delays).not.toContain(5000)
    },
    15000,
  )
})
