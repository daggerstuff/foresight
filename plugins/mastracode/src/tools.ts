import { createTool, z } from 'mastracode/plugin'

import {
  laneArgs,
  loadCredentials,
  mcpCall,
  type ForesightClientConfig,
} from './client.js'

function mapScope(scope?: string): string {
  if (scope === 'project') return 'arc'
  if (scope === 'global') return 'trait'
  if (scope === 'session') return 'session'
  return scope ?? 'arc'
}

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
    execute: async ({ query, max_memories }) => {
      // One load per call: laneArgs and the MCP request must read the same
      // credentials, and mcpCall must not resolve them a second time.
      const credentials = await loadCredentials(config)
      const res = await mcpCall(
        'inject_context',
        {
          conversation_text: query,
          max_memories: max_memories ?? 6,
          ...laneArgs(credentials),
        },
        config,
        credentials,
      )
      if (res === null) {
        return {
          error:
            'Failed to inject context: Foresight MCP server call failed or timed out.',
        }
      }
      return { context: res }
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
    execute: async ({ content, category, importance, scope, retention }) => {
      // One load per call: laneArgs and the MCP request must read the same
      // credentials, and mcpCall must not resolve them a second time.
      const credentials = await loadCredentials(config)
      const res = await mcpCall(
        'manage_memories',
        {
          action: 'store',
          content,
          category: category ?? 'decision',
          importance: importance ?? 0.9,
          scope: mapScope(scope),
          retention: retention ?? 'permanent',
          ...laneArgs(credentials),
        },
        config,
        credentials,
      )
      if (res === null) {
        return {
          error:
            'Failed to store memory: Foresight MCP server call failed or timed out.',
        }
      }
      return { result: res }
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
    execute: async ({ query, limit }) => {
      // One load per call: laneArgs and the MCP request must read the same
      // credentials, and mcpCall must not resolve them a second time.
      const credentials = await loadCredentials(config)
      const res = await mcpCall(
        'search_memories',
        {
          query,
          limit: limit ?? 10,
          ...laneArgs(credentials),
        },
        config,
        credentials,
      )
      if (res === null) {
        return {
          error:
            'Failed to search memories: Foresight MCP server call failed or timed out.',
        }
      }
      return { memories: res }
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
    execute: async ({ label }) => {
      // One load per call: laneArgs and the MCP request must read the same
      // credentials, and mcpCall must not resolve them a second time.
      const credentials = await loadCredentials(config)
      const res = await mcpCall(
        'manage_context_blocks',
        {
          action: label ? 'get' : 'list',
          label,
          ...laneArgs(credentials),
        },
        config,
        credentials,
      )
      if (res === null) {
        return {
          error:
            'Failed to load context blocks: Foresight MCP server call failed or timed out.',
        }
      }
      return { blocks: res }
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
    execute: async ({ label, content }) => {
      // One load per call: laneArgs and the MCP request must read the same
      // credentials, and mcpCall must not resolve them a second time.
      const credentials = await loadCredentials(config)
      const res = await mcpCall(
        'manage_context_blocks',
        {
          action: 'update',
          label,
          content,
          ...laneArgs(credentials),
        },
        config,
        credentials,
      )
      if (res === null) {
        return {
          error:
            'Failed to update context block: Foresight MCP server call failed or timed out.',
        }
      }
      return { result: res }
    },
  })

  const systemStatusTool = createTool({
    id: 'foresight_system_status',
    description:
      'Check Foresight server connection, memory counts, and subsystem health.',
    inputSchema: z.object({}),
    execute: async () => {
      const res = await mcpCall('get_system_status', {}, config)
      if (res === null) {
        return {
          error:
            'Failed to read system status: Foresight MCP server call failed or timed out.',
        }
      }
      return { status: res }
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
