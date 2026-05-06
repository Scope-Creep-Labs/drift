import type { EngineAdapter } from '../types/adapter'
import { MockAdapter } from './MockAdapter'
import { LangflowAdapter } from './LangflowAdapter'

let cached: EngineAdapter | null = null

export function getAdapter(): EngineAdapter {
  if (cached) return cached
  const engine = (import.meta.env.VITE_ENGINE ?? 'mock').toString()
  cached = engine === 'langflow' ? new LangflowAdapter() : new MockAdapter()
  return cached
}
