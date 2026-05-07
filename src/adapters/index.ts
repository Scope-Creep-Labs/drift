import type { EngineAdapter } from '../types/adapter'
import { MockAdapter } from './MockAdapter'
import { AgentAdapter } from './AgentAdapter'

let cached: EngineAdapter | null = null

export function getAdapter(): EngineAdapter {
  if (cached) return cached
  const engine = (import.meta.env.VITE_ENGINE ?? 'mock').toString().toLowerCase()
  cached = engine === 'agent' ? new AgentAdapter() : new MockAdapter()
  return cached
}

export function resetAdapter(): void {
  cached = null
}
