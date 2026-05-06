import type { RenderBlock } from './blocks'

export type PromptRequest = {
  prompt: string
  context?: {
    assetId?: string
    timeRange?: { start: string; end: string }
    investigationId?: string
  }
}

export type PromptResponse = {
  blocks: RenderBlock[]
  metadata?: {
    engine: string
    confidence?: number
    dataSources?: string[]
  }
}
