import type { EngineAdapter } from '../types/adapter'
import type { PromptRequest, PromptResponse } from '../types/prompt'
import { runScenario } from '../data/scenarios'

const MIN_DELAY_MS = 600
const MAX_DELAY_MS = 1500

export class MockAdapter implements EngineAdapter {
  async run(req: PromptRequest): Promise<PromptResponse> {
    const delay = MIN_DELAY_MS + Math.random() * (MAX_DELAY_MS - MIN_DELAY_MS)
    await new Promise((r) => setTimeout(r, delay))
    return runScenario(req.prompt)
  }
}
