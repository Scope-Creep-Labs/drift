import type { PromptRequest, PromptResponse } from './prompt'

export interface EngineAdapter {
  run(req: PromptRequest): Promise<PromptResponse>
}
