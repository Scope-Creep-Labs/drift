import type { EngineAdapter } from '../types/adapter'
import type { PromptRequest, PromptResponse } from '../types/prompt'

export class LangflowAdapter implements EngineAdapter {
  constructor(private readonly endpoint: string = import.meta.env.VITE_LANGFLOW_URL ?? '') {}

  async run(_req: PromptRequest): Promise<PromptResponse> {
    if (!this.endpoint) {
      throw new Error(
        'LangflowAdapter is not yet implemented. Set VITE_ENGINE=mock to use the mock adapter.',
      )
    }
    throw new Error('LangflowAdapter.run not yet implemented')
  }
}
