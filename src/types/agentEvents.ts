import type { RenderBlock } from './blocks'

export type AgentEvent =
  | { type: 'start'; engine?: string }
  | { type: 'thinking'; text: string }
  | { type: 'narrative'; text: string }
  | { type: 'tool_call'; id: string; name: string; args: unknown }
  | { type: 'tool_result'; id: string; name: string; summary: string; is_error: boolean }
  | { type: 'data'; ref: string; traces: unknown[] }
  | { type: 'block'; block: RenderBlock }
  | {
      type: 'metadata'
      metadata: {
        engine: string
        stop_reason?: string
        usage?: {
          input_tokens?: number
          output_tokens?: number
          cache_read_input_tokens?: number
          cache_creation_input_tokens?: number
        }
      }
    }
  | { type: 'done' }
  | { type: 'error'; error: string }

export type TraceEntry =
  | { kind: 'thinking'; text: string }
  | { kind: 'narrative'; text: string }
  | {
      kind: 'tool_call'
      id: string
      name: string
      args: unknown
      status: 'pending' | 'done' | 'error'
      summary?: string
    }
