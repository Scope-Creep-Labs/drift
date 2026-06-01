// Typed client for the operator-filters surface.
//
// Mirrors the agent-side remember_filter / promote_filter / forget_filter
// tools but takes user input from the Filters sidebar modal instead of
// going through the chat agent. Same cookie-based auth as the rest of
// the SPA.
import { apiBase } from './apiBase'

const FILTERS_BASE = apiBase() + '/filters'

export type FilterScope = {
  device?: string | null
  container?: string | null
  group?: string | null
  signal?: string | null
}

export type OperatorFilterRow = {
  id: string
  pattern: string
  scope: FilterScope
  reason: string
  visibility: 'private' | 'fleet'
  // True iff the calling operator created this row. Drives the UI's
  // delete / promote affordances — non-owners can see fleet filters but
  // can't revoke them.
  owned_by_me: boolean
  created_at: string
  last_applied_at: string | null
  apply_count: number
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${FILTERS_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}${text ? `: ${text}` : ''}`)
  }
  if (res.status === 204) return undefined as T
  return res.json() as Promise<T>
}

export const filtersApi = {
  list: () => api<OperatorFilterRow[]>(''),

  create: (body: { pattern: string; scope: FilterScope; reason: string }) =>
    api<OperatorFilterRow>('', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  promote: (id: string) =>
    api<OperatorFilterRow>(`/${encodeURIComponent(id)}/promote`, {
      method: 'POST',
    }),

  delete: (id: string) =>
    api<void>(`/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
}
