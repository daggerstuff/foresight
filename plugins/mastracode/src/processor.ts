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
      const { messages, systemMessages, messageList, requestContext } = args
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
