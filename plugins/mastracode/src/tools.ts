import { createTool, z } from 'mastracode/plugin';
import { mcpCall, type ForesightClientConfig } from './client.js';

export function createForesightTools(config?: ForesightClientConfig) {
  const injectContextTool = createTool({
    id: 'foresight_inject_context',
    description: 'Retrieve relevant memories, standing preferences, and architecture context for the given task or topic.',
    inputSchema: z.object({
      query: z.string().describe('The task description or query to retrieve context for'),
      max_memories: z.number().optional().default(6).describe('Maximum number of memories to surface'),
    }),
    execute: async ({ context }) => {
      const res = await mcpCall('inject_context', {
        conversation_text: context.query,
        max_memories: context.max_memories ?? 6,
        user_id: config?.userId || 'default',
      }, config);
      return { context: res || 'No specific memories found for query.' };
    },
  });

  const storeMemoryTool = createTool({
    id: 'foresight_store_memory',
    description: 'Persist an important technical fact, architectural decision, constraint, or user preference into Foresight.',
    inputSchema: z.object({
      content: z.string().describe('The knowledge, preference, or decision to store'),
      category: z.enum(['decision', 'preference', 'fact', 'run']).optional().default('decision'),
      importance: z.number().min(0).max(1).optional().default(0.9),
      scope: z.enum(['global', 'project', 'session']).optional().default('project'),
      retention: z.enum(['permanent', 'long_term', 'transient']).optional().default('permanent'),
    }),
    execute: async ({ context }) => {
      const res = await mcpCall('manage_memories', {
        action: 'store',
        content: context.content,
        category: context.category ?? 'decision',
        importance: context.importance ?? 0.9,
        scope: context.scope ?? 'project',
        retention: context.retention ?? 'permanent',
        user_id: config?.userId || 'default',
      }, config);
      return { result: res || 'Memory stored successfully.' };
    },
  });

  const searchMemoriesTool = createTool({
    id: 'foresight_search_memories',
    description: 'Search persistent memory using hybrid semantic vector and keyword search.',
    inputSchema: z.object({
      query: z.string().describe('Search query string'),
      limit: z.number().optional().default(10).describe('Maximum results to return'),
    }),
    execute: async ({ context }) => {
      const res = await mcpCall('search_memories', {
        query: context.query,
        limit: context.limit ?? 10,
        user_id: config?.userId || 'default',
      }, config);
      return { results: res || 'No memories matched.' };
    },
  });

  const getContextBlocksTool = createTool({
    id: 'foresight_get_context_blocks',
    description: 'List or retrieve standing distilled context blocks (such as user_preferences or coding_guidelines).',
    inputSchema: z.object({
      label: z.string().optional().describe('Specific context block label (e.g. user_preferences) or omit to list all'),
    }),
    execute: async ({ context }) => {
      const res = await mcpCall('manage_context_blocks', {
        action: context.label ? 'get' : 'list',
        label: context.label,
        user_id: config?.userId || 'default',
      }, config);
      return { blocks: res || 'No context blocks found.' };
    },
  });

  const updateContextBlockTool = createTool({
    id: 'foresight_update_context_block',
    description: 'Update or append content in a standing distilled context block (e.g. user_preferences).',
    inputSchema: z.object({
      label: z.string().describe('The context block label (e.g. user_preferences)'),
      content: z.string().describe('The content or rule to set or update'),
    }),
    execute: async ({ context }) => {
      const res = await mcpCall('manage_context_blocks', {
        action: 'update',
        label: context.label,
        content: context.content,
        user_id: config?.userId || 'default',
      }, config);
      return { result: res || `Context block '${context.label}' updated.` };
    },
  });

  const systemStatusTool = createTool({
    id: 'foresight_system_status',
    description: 'Check Foresight server connection, memory counts, and subsystem health.',
    inputSchema: z.object({}),
    execute: async () => {
      const res = await mcpCall('get_system_status', {}, config);
      return { status: res || 'Foresight server online.' };
    },
  });

  return {
    foresight_inject_context: { tool: injectContextTool },
    foresight_store_memory: { tool: storeMemoryTool },
    foresight_search_memories: { tool: searchMemoriesTool },
    foresight_get_context_blocks: { tool: getContextBlocksTool },
    foresight_update_context_block: { tool: updateContextBlockTool },
    foresight_system_status: { tool: systemStatusTool },
  };
}
