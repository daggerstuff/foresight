import type { InputProcessor, OutputProcessor } from 'mastracode/plugin'

import {
  autoCaptureTurn,
  fetchInjectContext,
  type ForesightClientConfig,
} from './client.js'

export const FORESIGHT_DIRECTIVES = `
## Foresight Persistent Memory Directives
You have access to the Foresight persistent memory system.
1. Apply the injected [FORESIGHT CONTEXT] memories and standing preferences naturally.
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

interface TextPart {
  type?: string
  text?: string
}

function extractTextFromParts(parts: unknown[]): string {
  if (!Array.isArray(parts)) return ''
  return parts
    .filter(
      (p): p is TextPart & { text: string } =>
        typeof p === 'object' &&
        p !== null &&
        'type' in p &&
        (p as TextPart).type === 'text' &&
        'text' in p &&
        typeof (p as TextPart).text === 'string',
    )
    .map((p) => p.text)
    .join(' ')
}

type MastraProcessInputArgs = Parameters<
  NonNullable<InputProcessor['processInput']>
>[0]
type MastraProcessOutputResultArgs = Parameters<
  NonNullable<OutputProcessor['processOutputResult']>
>[0]

function resolveThreadId(args: Record<string, unknown>): string {
  const reqCtx = args.requestContext as
    | { threadId?: string; sessionId?: string }
    | undefined
  return (
    reqCtx?.threadId ??
    reqCtx?.sessionId ??
    (typeof args.threadId === 'string' ? args.threadId : undefined) ??
    'mastracode-session'
  )
}

export function createForesightProcessor(
  config?: ForesightClientConfig,
): InputProcessor & OutputProcessor {
  return {
    id: 'foresight-context-processor',
    name: 'Foresight Context & Capture Processor',
    description:
      'Zero-touch continuity context injection and background turn capture.',

    async processInput(args: MastraProcessInputArgs) {
      const { messages, systemMessages } = args
      const threadId = resolveThreadId(args as unknown as Record<string, unknown>)
      const state = getSessionState(threadId)

      // Extract user text
      let userQuery = ''
      if (Array.isArray(messages) && messages.length > 0) {
        for (let i = messages.length - 1; i >= 0; i--) {
          const msg = messages[i] as unknown as {
            role?: string
            sender?: string
            content?:
              | string
              | {
                  content?: string
                  parts?: unknown[]
                }
          }
          if (msg && (msg.role === 'user' || msg.sender === 'user')) {
            if (
              typeof msg.content === 'object' &&
              msg.content !== null &&
              Array.isArray(msg.content.parts)
            ) {
              userQuery = extractTextFromParts(msg.content.parts)
            } else if (typeof msg.content === 'string') {
              userQuery = msg.content
            } else if (
              typeof msg.content === 'object' &&
              msg.content !== null &&
              typeof msg.content.content === 'string'
            ) {
              userQuery = msg.content.content
            }
            if (userQuery) break
          }
        }
      }

      state.lastUserQuery = userQuery

      // Ensure directives are present in systemMessages
      const sysList = Array.isArray(systemMessages) ? [...systemMessages] : []
      const hasDirectives = sysList.some((m) => {
        const item = m as unknown
        if (typeof item === 'string') {
          return item.includes('Foresight Persistent Memory Directives')
        }
        if (typeof item === 'object' && item !== null && 'content' in item) {
          const content = item.content
          return (
            typeof content === 'string' &&
            content.includes('Foresight Persistent Memory Directives')
          )
        }
        return false
      })

      if (!hasDirectives) {
        sysList.push({
          role: 'system',
          content: FORESIGHT_DIRECTIVES,
        } as unknown as (typeof sysList)[number])
      }

      // If query is new, fetch context
      if (userQuery && userQuery !== state.lastInjectedQuery) {
        try {
          const contextText = await fetchInjectContext(userQuery, config)
          if (contextText?.trim()) {
            state.lastInjectedQuery = userQuery
            state.lastInjectedAt = Date.now()
            sysList.push({
              role: 'system',
              content: `[FORESIGHT CONTINUITY CONTEXT]\n${contextText.trim()}\n[/FORESIGHT CONTINUITY CONTEXT]`,
            } as unknown as (typeof sysList)[number])
          }
        } catch (_) {}
      }

      return {
        messages,
        systemMessages: sysList,
      }
    },

    async processOutputResult(args: MastraProcessOutputResultArgs) {
      const { messages } = args
      const result = (args as unknown as { result?: { text?: string } }).result
      const threadId = resolveThreadId(args as unknown as Record<string, unknown>)
      const state = getSessionState(threadId)

      const userText = state.lastUserQuery
      let assistantText = ''

      if (result && typeof result.text === 'string' && result.text.trim()) {
        assistantText = result.text.trim()
      } else if (Array.isArray(messages) && messages.length > 0) {
        const last = messages[messages.length - 1] as unknown as {
          role?: string
          sender?: string
          content?:
            | string
            | {
                content?: string
                parts?: unknown[]
              }
        }
        if (
          last &&
          (last.role === 'assistant' || last.sender === 'assistant')
        ) {
          if (
            typeof last.content === 'object' &&
            last.content !== null &&
            Array.isArray(last.content.parts)
          ) {
            assistantText = extractTextFromParts(last.content.parts)
          } else if (typeof last.content === 'string') {
            assistantText = last.content
          } else if (
            typeof last.content === 'object' &&
            last.content !== null &&
            typeof last.content.content === 'string'
          ) {
            assistantText = last.content.content
          }
        }
      }

      if (userText && assistantText) {
        void autoCaptureTurn(threadId, userText, assistantText, config)
      }

      return messages ?? []
    },
  }
}
