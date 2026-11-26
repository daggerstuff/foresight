/**
 * Foresight HTTP & MCP Client for Mastra Code.
 *
 * Implementation lives in the shared plugin SDK (@foresight/plugin-sdk,
 * plugins/sdk/typescript); this module re-exports it so existing imports
 * keep working.
 */

export {
  autoCaptureTurn,
  captureText,
  captureTranscript,
  fetchInjectContext,
  mcpCall,
  parseSSEResult,
  postJson,
  postTelemetry,
} from '../../sdk/typescript/src/index.js'
export type {
  ForesightClientConfig,
  ForesightCredentials,
  PostJsonResult,
} from '../../sdk/typescript/src/index.js'
