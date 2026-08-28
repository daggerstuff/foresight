// foresight-autoinject — opencode plugin
//
// Truly hands-off persistent memory and continuity context injection.
// - Fast single-roundtrip stateless MCP 2.0 (2026-07-28) calls with fallback.
// - Auto-injects relevant memories and active guidance into system prompt.
// - Injects behavioral directives so models proactively call memory tools.
// - Background auto-capture: extracts key facts, decisions, and preferences
//   from completed turns without requiring manual user commands.

const MCP_URL = process.env.FORESIGHT_MCP_URL ?? 'http://127.0.0.1:8764/mcp'
const MCP_HEADERS = {
  'Content-Type': 'application/json',
  'Accept': 'application/json, text/event-stream',
}
const REQUEST_TIMEOUT_MS = 6000 // 6s timeout

// Parse SSE or JSON response body to extract JSON-RPC result
function parseSSEResult(body) {
  if (!body) return null
  try {
    const parsed = JSON.parse(body)
    if (parsed) return parsed
  } catch (_) {
    // Fall through to SSE line parsing
  }
  const lines = body.split('\n')
  for (const line of lines) {
    if (line.startsWith('data: ')) {
      try {
        return JSON.parse(line.slice(6))
      } catch (_) {}
    }
  }
  return null
}

async function mcpStatelessCall(toolName, args) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)

  try {
    const resp = await fetch(MCP_URL, {
      method: 'POST',
      headers: {
        ...MCP_HEADERS,
        'MCP-Protocol-Version': '2026-07-28',
        'Mcp-Method': 'tools/call',
      },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: Math.floor(Math.random() * 100000),
        method: 'tools/call',
        params: {
          name: toolName,
          arguments: args,
        },
      }),
      signal: controller.signal,
    })

    if (!resp.ok) return null
    const text = await resp.text()
    const parsed = parseSSEResult(text)
    if (!parsed || parsed.error || !parsed.result) return null

    if (Array.isArray(parsed.result.content)) {
      return parsed.result.content
        .filter((c) => c.type === 'text' && c.text)
        .map((c) => c.text)
        .join('\n')
    }
    return typeof parsed.result === 'string' ? parsed.result : null
  } catch (_) {
    return null
  } finally {
    clearTimeout(timer)
  }
}

// Fallback legacy 3-step handshake for older server versions
async function mcpLegacyCall(toolName, args) {
  try {
    const initResp = await fetch(MCP_URL, {
      method: 'POST',
      headers: MCP_HEADERS,
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'initialize',
        params: {
          protocolVersion: '2024-11-05',
          capabilities: {},
          clientInfo: { name: 'foresight-autoinject', version: '2.0' },
        },
      }),
    })
    if (!initResp.ok) return null
    const sessionId = initResp.headers.get('mcp-session-id')
    if (!sessionId) return null

    // Notify initialized
    await fetch(MCP_URL, {
      method: 'POST',
      headers: { ...MCP_HEADERS, 'Mcp-Session-Id': sessionId },
      body: JSON.stringify({
        jsonrpc: '2.0',
        method: 'notifications/initialized',
      }),
    }).catch(() => {})

    // Call tool
    const toolResp = await fetch(MCP_URL, {
      method: 'POST',
      headers: { ...MCP_HEADERS, 'Mcp-Session-Id': sessionId },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 2,
        method: 'tools/call',
        params: { name: toolName, arguments: args },
      }),
    })
    if (!toolResp.ok) return null
    const text = await toolResp.text()
    const parsed = parseSSEResult(text)
    if (!parsed || parsed.error || !parsed.result) return null

    if (Array.isArray(parsed.result.content)) {
      return parsed.result.content
        .filter((c) => c.type === 'text' && c.text)
        .map((c) => c.text)
        .join('\n')
    }
    return typeof parsed.result === 'string' ? parsed.result : null
  } catch (_) {
    return null
  }
}

async function callInjectContext(conversationText) {
  const args = {
    conversation_text: conversationText || '',
    max_memories: 5,
    min_relevance: 0.01,
    max_chars: 4000,
  }
  // Try modern stateless single-roundtrip first
  const fastResult = await mcpStatelessCall('inject_context', args)
  if (fastResult) return fastResult
  // Fallback to legacy
  return await mcpLegacyCall('inject_context', args)
}

async function callAutoCapture(sessionId, userText, assistantText) {
  if (!userText || userText.length < 5) return
  const messages = [{ role: 'user', content: userText }]
  if (assistantText) {
    messages.push({ role: 'assistant', content: assistantText.slice(0, 1000) })
  }
  const args = {
    session_id: sessionId,
    messages,
  }
  // Fire and forget (stateless)
  mcpStatelessCall('process_session_transcript', args).catch(() => {})
}

// --- Plugin State & Hooks ---------------------------------------------

const sessionState = new Map()
const FAILURE_COOLDOWN_MS = 15000

function getSessionState(sessionId) {
  if (!sessionState.has(sessionId)) {
    sessionState.set(sessionId, {
      lastUserMessage: '',
      lastAssistantMessage: '',
      lastInjectedFor: '',
      lastFailureAt: 0,
    })
  }
  return sessionState.get(sessionId)
}

function extractSessionId(_input, _ctx) {
  if (_input && (_input.sessionId || _input.session_id))
    return _input.sessionId ?? _input.session_id
  if (_ctx && (_ctx.sessionId || _ctx.session_id))
    return _ctx.sessionId ?? _ctx.session_id
  return 'default'
}

function extractUserQuery(_input, state) {
  if (_input?.prompt && typeof _input.prompt === 'string') {
    return _input.prompt
  }
  if (_input?.message && typeof _input.message === 'string') {
    return _input.message
  }
  if (_input?.messages && Array.isArray(_input.messages)) {
    const userMsgs = _input.messages.filter(
      (m) => m && (m.role === 'user' || m.sender === 'user'),
    )
    if (userMsgs.length > 0) {
      const last = userMsgs[userMsgs.length - 1]
      if (typeof last.content === 'string') return last.content
      if (Array.isArray(last.parts)) {
        return last.parts
          .filter((p) => p?.type === 'text' && p.text)
          .map((p) => p.text)
          .join(' ')
      }
    }
  }
  return state.lastUserMessage || ''
}

const AUTOCONTEXT_INSTRUCTIONS = `
## Foresight Persistent Memory Directives
You have access to the Foresight persistent memory system.
1. Apply the injected [FORESIGHT CONTEXT] memories and standing preferences naturally.
2. At the start of new tasks, on topic shifts, or before non-trivial planning/coding, proactively call \`inject_context\` with the current subject.
3. When the user expresses conventions, technical decisions, or preferences ('I prefer X', 'always use Y'), call \`manage_context_blocks(action='update', label='user_preferences', content='...')\` or \`manage_memories(action='store', ...)\` to persist them silently.
`

export const ForesightAutoInject = async (_ctx) => {
  return {
    'chat.message': async (_input, output) => {
      if (!output) return
      const sessionId = extractSessionId(_input, _ctx)
      const state = getSessionState(sessionId)

      let text = ''
      if (typeof output.content === 'string') {
        text = output.content
      } else if (Array.isArray(output.parts)) {
        text = output.parts
          .filter((p) => p?.type === 'text' && p.text)
          .map((p) => p.text)
          .join(' ')
      }

      if (text) {
        if (output.role === 'assistant' || output.sender === 'assistant') {
          state.lastAssistantMessage = text
          if (state.lastUserMessage) {
            callAutoCapture(sessionId, state.lastUserMessage, text)
          }
        } else {
          state.lastUserMessage = text
        }
      }
    },

    'experimental.chat.system.transform': async (_input, output) => {
      if (!output || !Array.isArray(output.system)) return

      const sessionId = extractSessionId(_input, _ctx)
      const state = getSessionState(sessionId)
      const query = extractUserQuery(_input, state).trim()

      // Always ensure behavioral instructions are present
      const hasDirectives = output.system.some(
        (s) =>
          typeof s === 'string' &&
          s.includes('Foresight Persistent Memory Directives'),
      )
      if (!hasDirectives) {
        output.system.push(AUTOCONTEXT_INSTRUCTIONS)
      }

      if (Date.now() - state.lastFailureAt < FAILURE_COOLDOWN_MS) return
      if (query && query === state.lastInjectedFor) return

      let contextText
      try {
        contextText = await callInjectContext(query)
      } catch (_) {
        state.lastFailureAt = Date.now()
        return
      }

      if (!contextText?.trim()) {
        state.lastFailureAt = Date.now()
        return
      }

      state.lastInjectedFor = query || 'standing'
      output.system.push(
        '[FORESIGHT CONTEXT]\n' + contextText + '\n[/FORESIGHT CONTEXT]',
      )
    },
  }
}

export default ForesightAutoInject
