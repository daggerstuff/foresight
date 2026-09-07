import {
  defineMastraCodePlugin,
  type MastraCodePluginContext,
} from 'mastracode/plugin'

import {
  autoCaptureTurn,
  fetchInjectContext,
  mcpCall,
  parseSSEResult,
  type ForesightClientConfig,
} from './client.js'
import {
  createForesightProcessor,
  FORESIGHT_DIRECTIVES,
} from './processor.js'
import { createForesightTools } from './tools.js'

export {
  autoCaptureTurn,
  createForesightProcessor,
  createForesightTools,
  fetchInjectContext,
  FORESIGHT_DIRECTIVES,
  mcpCall,
  parseSSEResult,
  type ForesightClientConfig,
}

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
    const serverUrl =
      typeof context.config.serverUrl === 'string' && context.config.serverUrl
        ? context.config.serverUrl
        : undefined
    const userId =
      typeof context.config.userId === 'string' && context.config.userId
        ? context.config.userId
        : undefined
    const clientConfig: ForesightClientConfig = {
      baseUrl:
        serverUrl ?? process.env.FORESIGHT_MCP_URL ?? 'http://127.0.0.1:8764',
      userId: userId ?? 'default',
    }
    return createForesightTools(clientConfig)
  },

  processors: (context: MastraCodePluginContext) => {
    const autoInject = context.config.autoInject !== false
    const autoCapture = context.config.autoCapture !== false

    if (!autoInject && !autoCapture) {
      return []
    }

    const serverUrl =
      typeof context.config.serverUrl === 'string' && context.config.serverUrl
        ? context.config.serverUrl
        : undefined
    const userId =
      typeof context.config.userId === 'string' && context.config.userId
        ? context.config.userId
        : undefined
    const clientConfig: ForesightClientConfig = {
      baseUrl:
        serverUrl ?? process.env.FORESIGHT_MCP_URL ?? 'http://127.0.0.1:8764',
      userId: userId ?? 'default',
    }

    return [createForesightProcessor(clientConfig)]
  },
})
