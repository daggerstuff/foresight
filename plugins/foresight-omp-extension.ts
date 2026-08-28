// @ts-nocheck
// Foresight ambient context and auto-capture extension for OMP (Oh My Pi / Pi agent)

import http from 'node:http'

const FORESIGHT_URL = process.env.FORESIGHT_HTTP_URL || 'http://127.0.0.1:8764'

function postJson(path: string, payload: any, timeoutMs = 3000): Promise<any> {
  return new Promise((resolve) => {
    try {
      const data = JSON.stringify(payload)
      const url = new URL(path, FORESIGHT_URL)
      const req = http.request(
        url,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Content-Length': Buffer.byteLength(data),
            'MCP-Protocol-Version': '2026-07-28',
            'Mcp-Method': 'tools/call',
          },
          timeout: timeoutMs,
        },
        (res) => {
          let body = ''
          res.on('data', (chunk) => (body += chunk))
          res.on('end', () => {
            try {
              resolve(JSON.parse(body))
            } catch {
              resolve(null)
            }
          })
        },
      )
      req.on('error', () => resolve(null))
      req.on('timeout', () => {
        req.destroy()
        resolve(null)
      })
      req.write(data)
      req.end()
    } catch {
      resolve(null)
    }
  })
}

export default function foresightOmpPlugin(pi: any) {
  // 1. When agent starts, retrieve and inject relevant context
  pi.on('agent_start', async (_event: any, ctx: any) => {
    try {
      const res = await postJson('/ui/api/inject', {
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
  pi.on('agent_end', async (event: any, ctx: any) => {
    try {
      const sessionId = ctx?.sessionId || ctx?.session?.id || 'omp-session'
      const messages = ctx?.messages || event?.messages || []
      if (messages && messages.length > 0) {
        void postJson('/mcp', {
          jsonrpc: '2.0',
          id: Date.now(),
          method: 'tools/call',
          params: {
            name: 'process_session_transcript',
            arguments: {
              session_id: sessionId,
              messages: messages.slice(-10),
            },
          },
        })
      }
    } catch {
      // Non-blocking fire-and-forget
    }
  })
}
