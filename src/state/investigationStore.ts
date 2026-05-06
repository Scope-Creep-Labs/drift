import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { nanoid } from 'nanoid'
import type { PromptResponse } from '../types/prompt'

export type Turn = {
  id: string
  prompt: string
  response: PromptResponse
  createdAt: string
}

export type Investigation = {
  id: string
  title: string
  turns: Turn[]
  createdAt: string
}

type Store = {
  investigations: Investigation[]
  activeId: string | null
  createInvestigation(): string
  setActive(id: string): void
  deleteInvestigation(id: string): void
  appendTurn(prompt: string, response: PromptResponse): string
  renameInvestigation(id: string, title: string): void
}

function deriveTitle(prompt: string): string {
  const trimmed = prompt.trim().replace(/\s+/g, ' ')
  if (trimmed.length <= 60) return trimmed
  return trimmed.slice(0, 57) + '…'
}

export const useInvestigationStore = create<Store>()(
  persist(
    (set, get) => ({
      investigations: [],
      activeId: null,

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

      appendTurn(prompt, response) {
        let activeId = get().activeId
        if (!activeId || !get().investigations.some((i) => i.id === activeId)) {
          activeId = get().createInvestigation()
        }
        const turnId = nanoid(10)
        set((s) => ({
          investigations: s.investigations.map((inv) =>
            inv.id === activeId
              ? {
                  ...inv,
                  title: inv.turns.length === 0 ? deriveTitle(prompt) : inv.title,
                  turns: [
                    ...inv.turns,
                    {
                      id: turnId,
                      prompt,
                      response,
                      createdAt: new Date().toISOString(),
                    },
                  ],
                }
              : inv,
          ),
        }))
        return turnId
      },

      renameInvestigation(id, title) {
        set((s) => ({
          investigations: s.investigations.map((i) => (i.id === id ? { ...i, title } : i)),
        }))
      },
    }),
    {
      name: 'drift.investigations.v1',
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
