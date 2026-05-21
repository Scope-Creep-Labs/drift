import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { nanoid } from 'nanoid'
import type { RenderBlock } from '../types/blocks'
import type { TraceEntry } from '../types/agentEvents'

export type TurnMetadata = {
  engine?: string
  stop_reason?: string
  usage?: {
    input_tokens?: number
    output_tokens?: number
    cache_read_input_tokens?: number
    cache_creation_input_tokens?: number
  }
}

export type Turn = {
  id: string
  prompt: string
  trace: TraceEntry[]
  blocks: RenderBlock[]
  metadata?: TurnMetadata
  createdAt: string
  error?: string
}

export type Investigation = {
  id: string
  title: string
  turns: Turn[]
  createdAt: string
}

export type StreamingTurn = {
  investigationId: string
  turnId: string
  prompt: string
  trace: TraceEntry[]
  blocks: RenderBlock[]
  metadata?: TurnMetadata
  startedAt: string
  error?: string
}

type Store = {
  investigations: Investigation[]
  activeId: string | null
  streaming: StreamingTurn | null

  // Report-export selection. Ephemeral; not persisted.
  selectMode: boolean
  selectedTurnIds: Set<string>

  createInvestigation(): string
  setActive(id: string): void
  deleteInvestigation(id: string): void
  renameInvestigation(id: string, title: string): void

  beginStream(prompt: string): { investigationId: string; turnId: string }
  appendThinking(text: string): void
  appendNarrative(text: string): void
  upsertToolCall(id: string, name: string, args: unknown): void
  finishToolCall(id: string, summary: string, isError: boolean): void
  addBlock(block: RenderBlock): void
  setStreamMetadata(metadata: TurnMetadata): void
  setStreamError(error: string): void
  finalizeStream(): void
  abortStream(): void

  enterSelectMode(): void
  exitSelectMode(): void
  toggleTurnSelected(id: string): void
  selectAllTurnsInActive(): void
}

function deriveTitle(prompt: string): string {
  const trimmed = prompt.trim().replace(/\s+/g, ' ')
  return trimmed.length <= 60 ? trimmed : trimmed.slice(0, 57) + '…'
}

function pushTraceText(
  trace: TraceEntry[],
  kind: 'thinking' | 'narrative',
  text: string,
): TraceEntry[] {
  const last = trace[trace.length - 1]
  if (last && last.kind === kind) {
    return [...trace.slice(0, -1), { kind, text: last.text + text }]
  }
  return [...trace, { kind, text }]
}

export const useInvestigationStore = create<Store>()(
  persist(
    (set, get) => ({
      investigations: [],
      activeId: null,
      streaming: null,
      selectMode: false,
      selectedTurnIds: new Set<string>(),

      createInvestigation() {
        const id = nanoid(10)
        set((s) => ({
          investigations: [
            {
              id,
              title: 'New investigation',
              turns: [],
              createdAt: new Date().toISOString(),
            },
            ...s.investigations,
          ],
          activeId: id,
        }))
        return id
      },

      setActive(id) {
        set({ activeId: id })
      },

      deleteInvestigation(id) {
        set((s) => {
          const remaining = s.investigations.filter((i) => i.id !== id)
          return {
            investigations: remaining,
            activeId: s.activeId === id ? remaining[0]?.id ?? null : s.activeId,
          }
        })
      },

      renameInvestigation(id, title) {
        set((s) => ({
          investigations: s.investigations.map((i) => (i.id === id ? { ...i, title } : i)),
        }))
      },

      beginStream(prompt) {
        let activeId = get().activeId
        if (!activeId || !get().investigations.some((i) => i.id === activeId)) {
          activeId = get().createInvestigation()
        }
        const turnId = nanoid(10)
        set({
          streaming: {
            investigationId: activeId!,
            turnId,
            prompt,
            trace: [],
            blocks: [],
            startedAt: new Date().toISOString(),
          },
        })
        return { investigationId: activeId!, turnId }
      },

      appendThinking(text) {
        set((s) =>
          s.streaming
            ? { streaming: { ...s.streaming, trace: pushTraceText(s.streaming.trace, 'thinking', text) } }
            : s,
        )
      },

      appendNarrative(text) {
        set((s) =>
          s.streaming
            ? { streaming: { ...s.streaming, trace: pushTraceText(s.streaming.trace, 'narrative', text) } }
            : s,
        )
      },

      upsertToolCall(id, name, args) {
        set((s) => {
          if (!s.streaming) return s
          const exists = s.streaming.trace.some((t) => t.kind === 'tool_call' && t.id === id)
          if (exists) return s
          return {
            streaming: {
              ...s.streaming,
              trace: [...s.streaming.trace, { kind: 'tool_call', id, name, args, status: 'pending' }],
            },
          }
        })
      },

      finishToolCall(id, summary, isError) {
        set((s) => {
          if (!s.streaming) return s
          return {
            streaming: {
              ...s.streaming,
              trace: s.streaming.trace.map((t) =>
                t.kind === 'tool_call' && t.id === id
                  ? { ...t, status: isError ? 'error' : 'done', summary }
                  : t,
              ),
            },
          }
        })
      },

      addBlock(block) {
        set((s) => {
          if (!s.streaming) return s
          // Live charts replace in place by chart_key. When the agent
          // mutates an existing live chart (different refresh, more
          // traces, etc.), we strip any prior live_chart with the same
          // chart_key from earlier turns in the same investigation
          // *and* from the current streaming buffer, then append the
          // fresh one. The LiveChartBlock component identifies by
          // `live:${chart_key}` as its React key, so the same instance
          // is reused — preserving Plotly zoom/hover/axes.
          if (block.type === 'live_chart') {
            const chartKey = block.chart_key
            const investigations = s.investigations.map((inv) =>
              inv.id === s.streaming!.investigationId
                ? {
                    ...inv,
                    turns: inv.turns.map((t) => ({
                      ...t,
                      blocks: t.blocks.filter(
                        (b) => !(b.type === 'live_chart' && b.chart_key === chartKey),
                      ),
                    })),
                  }
                : inv,
            )
            const streamingBlocks = s.streaming.blocks.filter(
              (b) => !(b.type === 'live_chart' && b.chart_key === chartKey),
            )
            return {
              investigations,
              streaming: { ...s.streaming, blocks: [...streamingBlocks, block] },
            }
          }
          return { streaming: { ...s.streaming, blocks: [...s.streaming.blocks, block] } }
        })
      },

      setStreamMetadata(metadata) {
        set((s) => (s.streaming ? { streaming: { ...s.streaming, metadata } } : s))
      },

      setStreamError(error) {
        set((s) => (s.streaming ? { streaming: { ...s.streaming, error } } : s))
      },

      finalizeStream() {
        const { streaming, investigations } = get()
        if (!streaming) return
        const turn: Turn = {
          id: streaming.turnId,
          prompt: streaming.prompt,
          trace: streaming.trace,
          blocks: streaming.blocks,
          metadata: streaming.metadata,
          error: streaming.error,
          createdAt: streaming.startedAt,
        }
        set({
          investigations: investigations.map((inv) =>
            inv.id === streaming.investigationId
              ? {
                  ...inv,
                  title: inv.turns.length === 0 ? deriveTitle(streaming.prompt) : inv.title,
                  turns: [...inv.turns, turn],
                }
              : inv,
          ),
          streaming: null,
        })
      },

      abortStream() {
        set({ streaming: null })
      },

      enterSelectMode() {
        set({ selectMode: true, selectedTurnIds: new Set<string>() })
      },

      exitSelectMode() {
        set({ selectMode: false, selectedTurnIds: new Set<string>() })
      },

      toggleTurnSelected(id) {
        set((s) => {
          const next = new Set(s.selectedTurnIds)
          if (next.has(id)) next.delete(id)
          else next.add(id)
          return { selectedTurnIds: next }
        })
      },

      selectAllTurnsInActive() {
        set((s) => {
          const active = s.investigations.find((i) => i.id === s.activeId)
          return { selectedTurnIds: new Set(active?.turns.map((t) => t.id) ?? []) }
        })
      },
    }),
    {
      name: 'drift.investigations.v2',
      partialize: (s) => ({
        investigations: s.investigations,
        activeId: s.activeId,
      }),
    },
  ),
)

export function useActiveInvestigation(): Investigation | null {
  return useInvestigationStore((s) => {
    const id = s.activeId
    if (!id) return null
    return s.investigations.find((i) => i.id === id) ?? null
  })
}
