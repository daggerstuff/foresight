import {
  createTool,
  defineMastraCodePlugin,
  z,
  type InputProcessor,
  type OutputProcessor,
  type MastraCodePluginContext,
} from 'mastracode/plugin'

/**
 * ==============================================================================
 * Foresight HTTP & MCP Client
 * ==============================================================================
 */

const DEFAULT_FORESIGHT_URL =
  process.env.FORESIGHT_MCP_URL ?? 'http://127.0.0.1:8764'
const DEFAULT_TIMEOUT_MS = 5000

export interface ForesightClientConfig {
  baseUrl?: string
  userId?: string
  timeoutMs?: number
}

function parseSSEResult(body: string): any {
  if (!body) return null
  try {
    const parsed = JSON.parse(body)
    if (parsed) return parsed
  } catch (_) {}

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

export async function mcpCall(
  toolName: string,
  args: Record<string, unknown>,
  config?: ForesightClientConfig,
): Promise<string | null> {
  const baseUrl = config?.baseUrl || DEFAULT_FORESIGHT_URL
  const timeoutMs = config?.timeoutMs || DEFAULT_TIMEOUT_MS
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
      },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: Math.floor(Math.random() * 1000000),
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
        .filter((c: any) => c && c.type === 'text' && c.text)
        .map((c: any) => c.text)
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
  const baseUrl = config?.baseUrl || DEFAULT_FORESIGHT_URL
  const timeoutMs = config?.timeoutMs || DEFAULT_TIMEOUT_MS

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
      const data = await resp.json()
      if (data && typeof data.formatted === 'string' && data.formatted.trim()) {
        return data.formatted.trim()
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
      user_id: config?.userId || 'default',
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
  mcpCall(
    'process_session_transcript',
    {
      session_id: sessionId || 'mastracode-session',
      messages,
      user_id: config?.userId || 'default',
    },
    config,
  ).catch(() => {})
}

/**
 * ==============================================================================
 * Foresight Directives & Processors
 * ==============================================================================
 */

export const FORESIGHT_DIRECTIVES = `
## Foresight Persistent Memory Directives
You have access to the Foresight persistent memory system.
1. Apply the injected [FORESIGHT CONTINUITY CONTEXT] memories and standing preferences naturally.
2. At the start of new tasks, on topic shifts, or before non-trivial planning/coding, proactively call \`foresight_inject_context\` with the current subject.
3. When the user expresses conventions, technical decisions, or preferences ('I prefer X', 'always use Y'), call \`foresight_update_context_block\` (label='user_preferences') or \`foresight_store_memory\` to persist them silently.
`.trim()

interface SessionMemoryState {
  lastUserQuery: string
  lastInjectedQuery: string
  lastInjectedAt: number
}

const sessionStates = new Map<string, SessionMemoryState>()

function getSessionState(sessionId: string): SessionMemoryState {
  if (!sessionStates.has(sessionId)) {
    sessionStates.set(sessionId, {
      lastUserQuery: '',
      lastInjectedQuery: '',
      lastInjectedAt: 0,
    })
  }
  return sessionStates.get(sessionId)!
}

function extractTextFromParts(parts: any[]): string {
  if (!Array.isArray(parts)) return ''
  return parts
    .filter((p) => p && p.type === 'text' && typeof p.text === 'string')
    .map((p) => p.text)
    .join(' ')
}

export function createForesightProcessor(
  config?: ForesightClientConfig,
): InputProcessor & OutputProcessor {
  return {
    id: 'foresight-context-processor',
    name: 'Foresight Context & Capture Processor',
    description:
      'Zero-touch continuity context injection and background turn capture.',

    async processInput(args: any) {
      const { messages, systemMessages, requestContext } = args
      const threadId =
        requestContext?.threadId ||
        requestContext?.sessionId ||
        'mastracode-session'
      const state = getSessionState(threadId)

      // Extract user text
      let userQuery = ''
      if (Array.isArray(messages) && messages.length > 0) {
        for (let i = messages.length - 1; i >= 0; i--) {
          const msg = messages[i]
          if (msg && (msg.role === 'user' || msg.sender === 'user')) {
            if (msg.content?.parts) {
              userQuery = extractTextFromParts(msg.content.parts)
            } else if (typeof msg.content === 'string') {
              userQuery = msg.content
            } else if (typeof msg.content?.content === 'string') {
              userQuery = msg.content.content
            }
            if (userQuery) break
          }
        }
      }

      state.lastUserQuery = userQuery

      // Ensure directives are present in systemMessages
      const sysList: any[] = Array.isArray(systemMessages) ? systemMessages : []
      const hasDirectives = sysList.some(
        (m) =>
          (typeof m === 'string' &&
            m.includes('Foresight Persistent Memory Directives')) ||
          (typeof m?.content === 'string' &&
            m.content.includes('Foresight Persistent Memory Directives')),
      )

      if (!hasDirectives) {
        sysList.push({
          role: 'system',
          content: FORESIGHT_DIRECTIVES,
        })
      }

      // If query is new, fetch context
      if (userQuery && userQuery !== state.lastInjectedQuery) {
        try {
          const contextText = await fetchInjectContext(userQuery, config)
          if (contextText && contextText.trim()) {
            state.lastInjectedQuery = userQuery
            state.lastInjectedAt = Date.now()
            sysList.push({
              role: 'system',
              content: `[FORESIGHT CONTINUITY CONTEXT]\n${contextText.trim()}\n[/FORESIGHT CONTINUITY CONTEXT]`,
            })
          }
        } catch (_) {}
      }

      return {
        messages,
        systemMessages: sysList,
      }
    },

    async processOutputResult(args: any) {
      const { result, messages, requestContext } = args
      const threadId =
        requestContext?.threadId ||
        requestContext?.sessionId ||
        'mastracode-session'
      const state = getSessionState(threadId)

      const userText = state.lastUserQuery
      let assistantText = ''

      if (result && typeof result.text === 'string' && result.text.trim()) {
        assistantText = result.text.trim()
      } else if (Array.isArray(messages) && messages.length > 0) {
        const last = messages[messages.length - 1]
        if (
          last &&
          (last.role === 'assistant' || last.sender === 'assistant')
        ) {
          if (last.content?.parts) {
            assistantText = extractTextFromParts(last.content.parts)
          } else if (typeof last.content === 'string') {
            assistantText = last.content
          } else if (typeof last.content?.content === 'string') {
            assistantText = last.content.content
          }
        }
      }

      if (userText && assistantText) {
        autoCaptureTurn(threadId, userText, assistantText, config)
      }

      return []
    },
  }
}

/**
 * ==============================================================================
 * Foresight Native Tools
 * ==============================================================================
 */

export function createForesightTools(config?: ForesightClientConfig) {
  const injectContextTool = createTool({
    id: 'foresight_inject_context',
    description:
      'Retrieve relevant memories, standing preferences, and architecture context for the given task or topic.',
    inputSchema: z.object({
      query: z
        .string()
        .describe('The task description or query to retrieve context for'),
      max_memories: z
        .number()
        .optional()
        .default(6)
        .describe('Maximum number of memories to surface'),
    }),
    execute: async (input: any) => {
      const query = input?.query ?? input?.context?.query ?? ''
      const max_memories =
        input?.max_memories ?? input?.context?.max_memories ?? 6
      const res = await mcpCall(
        'inject_context',
        {
          conversation_text: query,
          max_memories,
          user_id: config?.userId || 'default',
        },
        config,
      )
      return { context: res || 'No specific memories found for query.' }
    },
  })

  const storeMemoryTool = createTool({
    id: 'foresight_store_memory',
    description:
      'Persist an important technical fact, architectural decision, constraint, or user preference into Foresight.',
    inputSchema: z.object({
      content: z
        .string()
        .describe('The knowledge, preference, or decision to store'),
      category: z
        .enum(['decision', 'preference', 'fact', 'run'])
        .optional()
        .default('decision'),
      importance: z.number().min(0).max(1).optional().default(0.9),
      scope: z
        .enum(['global', 'project', 'session'])
        .optional()
        .default('project'),
      retention: z
        .enum(['permanent', 'long_term', 'transient'])
        .optional()
        .default('permanent'),
    }),
    execute: async (input: any) => {
      const content = input?.content ?? input?.context?.content ?? ''
      const category = input?.category ?? input?.context?.category ?? 'decision'
      const importance = input?.importance ?? input?.context?.importance ?? 0.9
      const scope = input?.scope ?? input?.context?.scope ?? 'project'
      const retention =
        input?.retention ?? input?.context?.retention ?? 'permanent'
      const res = await mcpCall(
        'manage_memories',
        {
          action: 'store',
          content,
          category,
          importance,
          scope,
          retention,
          user_id: config?.userId || 'default',
        },
        config,
      )
      return { result: res || 'Memory stored successfully.' }
    },
  })

  const searchMemoriesTool = createTool({
    id: 'foresight_search_memories',
    description:
      'Search persistent memory using hybrid semantic vector and keyword search.',
    inputSchema: z.object({
      query: z.string().describe('Search query string'),
      limit: z
        .number()
        .optional()
        .default(10)
        .describe('Maximum results to return'),
    }),
    execute: async (input: any) => {
      const query = input?.query ?? input?.context?.query ?? ''
      const limit = input?.limit ?? input?.context?.limit ?? 10
      const res = await mcpCall(
        'search_memories',
        {
          query,
          limit,
          user_id: config?.userId || 'default',
        },
        config,
      )
      return { results: res || 'No memories matched.' }
    },
  })

  const getContextBlocksTool = createTool({
    id: 'foresight_get_context_blocks',
    description:
      'List or retrieve standing distilled context blocks (such as user_preferences or coding_guidelines).',
    inputSchema: z.object({
      label: z
        .string()
        .optional()
        .describe(
          'Specific context block label (e.g. user_preferences) or omit to list all',
        ),
    }),
    execute: async (input: any) => {
      const label = input?.label ?? input?.context?.label
      const res = await mcpCall(
        'manage_context_blocks',
        {
          action: label ? 'get' : 'list',
          label,
          user_id: config?.userId || 'default',
        },
        config,
      )
      return { blocks: res || 'No context blocks found.' }
    },
  })

  const updateContextBlockTool = createTool({
    id: 'foresight_update_context_block',
    description:
      'Update or append content in a standing distilled context block (e.g. user_preferences).',
    inputSchema: z.object({
      label: z
        .string()
        .describe('The context block label (e.g. user_preferences)'),
      content: z.string().describe('The content or rule to set or update'),
    }),
    execute: async (input: any) => {
      const label = input?.label ?? input?.context?.label ?? ''
      const content = input?.content ?? input?.context?.content ?? ''
      const res = await mcpCall(
        'manage_context_blocks',
        {
          action: 'update',
          label,
          content,
          user_id: config?.userId || 'default',
        },
        config,
      )
      return { result: res || `Context block '${label}' updated.` }
    },
  })

  const systemStatusTool = createTool({
    id: 'foresight_system_status',
    description:
      'Check Foresight server connection, memory counts, and subsystem health.',
    inputSchema: z.object({}),
    execute: async () => {
      const res = await mcpCall('get_system_status', {}, config)
      return { status: res || 'Foresight server online.' }
    },
  })

  return {
    foresight_inject_context: { tool: injectContextTool },
    foresight_store_memory: { tool: storeMemoryTool },
    foresight_search_memories: { tool: searchMemoriesTool },
    foresight_get_context_blocks: { tool: getContextBlocksTool },
    foresight_update_context_block: { tool: updateContextBlockTool },
    foresight_system_status: { tool: systemStatusTool },
  }
}

/**
 * ==============================================================================
 * Plugin Export
 * ==============================================================================
 */

export default defineMastraCodePlugin({
  id: 'foresight',
  name: 'Foresight Persistent Memory',
  version: '1.0.0',
  description:
    'Hands-off persistent memory, continuity context injection, and background distillation for Mastra Code.',

  config: {
    serverUrl: {
      type: 'string',
      label: 'Foresight Server URL',
      description:
        'Base URL for the Foresight server or MCP endpoint (e.g. http://127.0.0.1:8764)',
      default: 'http://127.0.0.1:8764',
    },
    userId: {
      type: 'string',
      label: 'User ID',
      description:
        'User identifier for tenant/user isolation (e.g. default or your username)',
      default: 'default',
    },
    autoInject: {
      type: 'boolean',
      label: 'Auto-inject Context',
      description:
        'Automatically inject relevant memories and active context blocks before each turn',
      default: true,
    },
    autoCapture: {
      type: 'boolean',
      label: 'Auto-capture Turns',
      description:
        'Automatically distill facts, decisions, and preferences from completed turns in background',
      default: true,
    },
  },

  instructions: FORESIGHT_DIRECTIVES,

  tools: (context: MastraCodePluginContext) => {
    const clientConfig: ForesightClientConfig = {
      baseUrl:
        (context.config.serverUrl as string) ||
        process.env.FORESIGHT_MCP_URL ||
        'http://127.0.0.1:8764',
      userId: (context.config.userId as string) || 'default',
    }
    return createForesightTools(clientConfig)
  },

  processors: (context: MastraCodePluginContext) => {
    const autoInject = context.config.autoInject !== false
    const autoCapture = context.config.autoCapture !== false

    if (!autoInject && !autoCapture) {
      return []
    }

    const clientConfig: ForesightClientConfig = {
      baseUrl:
        (context.config.serverUrl as string) ||
        process.env.FORESIGHT_MCP_URL ||
        'http://127.0.0.1:8764',
      userId: (context.config.userId as string) || 'default',
    }

    return [createForesightProcessor(clientConfig)]
  },
})
