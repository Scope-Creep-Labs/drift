import type { EngineAdapter } from '../types/adapter'
import type { PromptRequest } from '../types/prompt'
import type { AgentEvent } from '../types/agentEvents'
import { runScenario } from '../data/scenarios'

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms))

function blockDelay(type: string): number {
  switch (type) {
    case 'markdown':
      return 220 + Math.random() * 320
    case 'metric':
      return 110 + Math.random() * 140
    case 'chart':
      return 650 + Math.random() * 600
    case 'table':
      return 380 + Math.random() * 280
    case 'timeline':
      return 320 + Math.random() * 220
    default:
      return 200
  }
}

export class MockAdapter implements EngineAdapter {
  async *stream(req: PromptRequest, signal?: AbortSignal): AsyncIterable<AgentEvent> {
    yield { type: 'start', engine: 'mock' }
    yield { type: 'narrative', text: `Routing prompt: "${req.prompt}"` }
    await sleep(220)
    if (signal?.aborted) return

    yield { type: 'thinking', text: 'Selecting scenario based on keywords… ' }
    await sleep(280)

    const response = runScenario(req.prompt)

    for (const block of response.blocks) {
      await sleep(blockDelay(block.type))
      if (signal?.aborted) return
      yield { type: 'block', block }
    }

    yield { type: 'metadata', metadata: response.metadata ?? { engine: 'mock' } }
    yield { type: 'done' }
  }
}
