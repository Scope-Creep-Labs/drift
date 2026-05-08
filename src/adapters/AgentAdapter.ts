import type { EngineAdapter } from '../types/adapter'
import type { PromptRequest } from '../types/prompt'
import type { AgentEvent } from '../types/agentEvents'
import { parseSSE } from '../lib/sseParser'

// When unset (or set to empty string by an ARG default), derive from Vite's
// BASE_URL so the agent endpoint follows the app's deployment subroute
// (e.g. base '/drift/' → API_BASE '/drift/api'). `||` (not `??`) is intentional
// so that an empty string also triggers the fallback.
const API_BASE: string =
  import.meta.env.VITE_API_BASE || `${import.meta.env.BASE_URL.replace(/\/$/, '')}/api`

export class AgentAdapter implements EngineAdapter {
  constructor(private readonly base: string = API_BASE) {}

  async *stream(req: PromptRequest, signal?: AbortSignal): AsyncIterable<AgentEvent> {
    const res = await fetch(`${this.base}/investigate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify(req),
      signal,
    })

    if (!res.ok || !res.body) {
      const text = await res.text().catch(() => '')
      yield { type: 'error', error: `agent http ${res.status}: ${text || res.statusText}` }
      yield { type: 'done' }
      return
    }

    const reader = res.body.getReader()
    try {
      for await (const frame of parseSSE(reader)) {
        const ev = frameToEvent(frame.event, frame.data)
        if (ev) yield ev
      }
    } finally {
      reader.releaseLock()
    }
  }
}

function frameToEvent(event: string, raw: string): AgentEvent | null {
  let data: any
  try {
    data = raw ? JSON.parse(raw) : {}
  } catch {
    return { type: 'error', error: `malformed sse data for event '${event}'` }
  }
  switch (event) {
    case 'start':
      return { type: 'start', engine: data.engine }
    case 'thinking':
      return { type: 'thinking', text: data.text ?? '' }
    case 'narrative':
      return { type: 'narrative', text: data.text ?? '' }
    case 'tool_call':
      return { type: 'tool_call', id: data.id, name: data.name, args: data.args }
    case 'tool_result':
      return {
        type: 'tool_result',
        id: data.id,
        name: data.name,
        summary: data.summary ?? '',
        is_error: !!data.is_error,
      }
    case 'data':
      return { type: 'data', ref: data.ref, traces: data.traces ?? [] }
    case 'block':
      return { type: 'block', block: data }
    case 'metadata':
      return { type: 'metadata', metadata: data.metadata ?? data }
    case 'done':
      return { type: 'done' }
    case 'error':
      return { type: 'error', error: data.error ?? 'unknown error' }
    default:
      return null
  }
}
