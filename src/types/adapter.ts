import type { PromptRequest } from './prompt'
import type { AgentEvent } from './agentEvents'

export interface EngineAdapter {
  stream(req: PromptRequest, signal?: AbortSignal): AsyncIterable<AgentEvent>
}
