// foresight-autoinject — opencode plugin
//
// Hands-off context injection from Foresight into the system prompt.
// Replaces the manual `foresight_inject_context` MCP tool call that agents
// were supposed to fire at conversation start but never did.
//
// Flow:
//   1. chat.message hook captures the latest user message text
//   2. experimental.chat.system.transform hook (fires before every LLM
//      request) calls Foresight inject_context via MCP HTTP transport,
//      appends the result to the system prompt
//
// This makes the "subconscious" context-block system truly hands-off:
// context blocks, relevant memories, and auto-captured triggers all
// surface automatically without the agent remembering to call a tool.
//
// Requirements:
//   - Foresight MCP server running on http://127.0.0.1:8764/mcp
//   - opencode.json mcp.foresight config (already present)
//
// Non-fatal: if Foresight is down or returns an error, the plugin skips
// silently — it never blocks the LLM request.
//
// Hook mapping (same pattern as caveman/plugin.js):
//   - chat.message: capture user message text
//   - experimental.chat.system.transform: inject context into system prompt

// --- MCP HTTP client (minimal, no deps) -------------------------------
//
// The Foresight MCP server uses Streamable HTTP transport (SSE responses).
// Protocol:
//   1. POST initialize → get session ID from mcp-session-id header
//   2. POST notifications/initialized → notify
//   3. POST tools/call → get tool result
//
// SSE response format: lines of `event: message\ndata: {json}` — we extract
// the JSON from the `data:` line.

const MCP_URL = process.env.FORESIGHT_MCP_URL || 'http://127.0.0.1:8764/mcp';
const MCP_HEADERS = {
  'Content-Type': 'application/json',
  'Accept': 'application/json, text/event-stream',
};
const REQUEST_TIMEOUT_MS = 8000; // 8s — Foresight may do DB queries

// Parse SSE response body to extract the JSON-RPC result.
// Format: `event: message\ndata: {"jsonrpc":"2.0",...}`
function parseSSE(body) {
  try {
    return JSON.parse(body);
  } catch (_) {
    // not plain JSON — fall through to SSE parsing
  }
  const lines = body.split('\n');
  for (const line of lines) {
    if (line.startsWith('data: ')) {
      try {
        return JSON.parse(line.slice(6));
      } catch (_) {
        // ignore parse errors on non-data lines
      }
    }
  }
  return null;
}

async function mcpCall(method, params, sessionId) {
  const headers = { ...MCP_HEADERS };
  if (sessionId) headers['Mcp-Session-Id'] = sessionId;

  const body = JSON.stringify({
    jsonrpc: '2.0',
    id: Math.floor(Math.random() * 100000),
    method,
    ...(params ? { params } : {}),
  });

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);

  try {
    const resp = await fetch(MCP_URL, {
      method: 'POST',
      headers,
      body,
      signal: controller.signal,
    });
    const text = await resp.text();
    return { ok: resp.ok, sessionId: resp.headers.get('mcp-session-id'), text };
  } finally {
    clearTimeout(timer);
  }
}

// Full MCP tool call: initialize → notify → tools/call
// Returns the tool result text, or null on any failure.
async function callInjectContext(conversationText) {
  let sessionId;

  // 1. Initialize
  const init = await mcpCall('initialize', {
    protocolVersion: '2024-11-05',
    capabilities: {},
    clientInfo: { name: 'foresight-autoinject', version: '1.0' },
  });
  if (!init || !init.ok) return null;
  sessionId = init.sessionId;
  if (!sessionId) return null;

  const initResult = parseSSE(init.text);
  if (!initResult || initResult.error) return null;

  // 2. Notify initialized (no response expected)
  await mcpCall('notifications/initialized', null, sessionId);

  // 3. Call inject_context
  const toolResp = await mcpCall('tools/call', {
    name: 'inject_context',
    arguments: {
      conversation_text: conversationText,
      max_memories: 5,
      min_relevance: 0.15,
      max_chars: 4000, // bounded: enables lane-based budget (STATIC>DYNAMIC>MEMORIES>BLOCKS>SAFETY)
    },
  }, sessionId);
  if (!toolResp || !toolResp.ok) return null;

  const result = parseSSE(toolResp.text);
  if (!result || result.error || !result.result) return null;

  // Extract text from content array
  if (result.result.content && Array.isArray(result.result.content)) {
    return result.result.content
      .filter((c) => c.type === 'text' && c.text)
      .map((c) => c.text)
      .join('\n');
  }
  return null;
}

// --- Plugin hooks -----------------------------------------------------

const sessionState = new Map();
const FAILURE_COOLDOWN_MS = 30000;

function getSessionState(sessionId) {
  if (!sessionState.has(sessionId)) {
    sessionState.set(sessionId, { lastUserMessage: '', lastInjectedFor: '', lastFailureAt: 0 });
  }
  return sessionState.get(sessionId);
}

function extractSessionId(_input, _ctx) {
  if (_input && (_input.sessionId || _input.session_id)) return _input.sessionId || _input.session_id;
  if (_ctx && (_ctx.sessionId || _ctx.session_id)) return _ctx.sessionId || _ctx.session_id;
  return 'default';
}

export const ForesightAutoInject = async (_ctx) => {
  return {
    'chat.message': async (_input, output) => {
      if (!output || !output.parts) return;
      const sessionId = extractSessionId(_input, _ctx);
      const state = getSessionState(sessionId);
      for (const part of output.parts) {
        if (part && part.type === 'text' && part.text) {
          state.lastUserMessage = part.text;
          break;
        }
      }
    },

    'experimental.chat.system.transform': async (_input, output) => {
      if (!output || !Array.isArray(output.system)) return;

      const sessionId = extractSessionId(_input, _ctx);
      const state = getSessionState(sessionId);
      const msg = state.lastUserMessage.trim();
      if (!msg) return;

      if (msg === state.lastInjectedFor) return;

      if (Date.now() - state.lastFailureAt < FAILURE_COOLDOWN_MS) return;

      let contextText;
      try {
        contextText = await callInjectContext(msg);
      } catch (_) {
        state.lastFailureAt = Date.now();
        return;
      }

      if (!contextText || !contextText.trim()) {
        state.lastFailureAt = Date.now();
        return;
      }

      state.lastInjectedFor = msg;

      output.system.push(
        '[FORESIGHT CONTEXT]\n' + contextText + '\n[/FORESIGHT CONTEXT]'
      );
    },
  };
};

export default ForesightAutoInject;
