/**
 * Foresight HTTP & MCP Client for Mastra Code.
 *
 * Implementation lives in the shared plugin SDK (@foresight/plugin-sdk,
 * plugins/sdk/typescript); this module re-exports it so existing imports
 * keep working.
 */

export {
  captureText,
  captureTranscript,
  fetchInjectContext,
  laneArgs,
  loadCredentials,
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

import {
  autoCaptureTurn as sdkAutoCaptureTurn,
  type ForesightClientConfig,
} from '../../sdk/typescript/src/index.js'

export const MASTRACODE_SESSION_FALLBACK = 'mastracode-session'

export async function autoCaptureTurn(
  sessionId: string,
  userMessage: string,
  assistantMessage: string,
  config: ForesightClientConfig = {},
): Promise<void> {
  await sdkAutoCaptureTurn(sessionId, userMessage, assistantMessage, {
    sessionFallback: MASTRACODE_SESSION_FALLBACK,
    ...config,
  })
}
