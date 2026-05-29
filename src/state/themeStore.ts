import { create } from 'zustand'

// User-facing setting: `'system'` follows `prefers-color-scheme`,
// `'light'` and `'dark'` are explicit overrides.
export type ThemeMode = 'light' | 'dark' | 'system'

// `resolvedMode` is what's actually rendered. When `mode === 'system'`,
// the resolved value tracks the OS preference and updates when the
// media query fires. When `mode` is explicit, `resolvedMode` mirrors it.
export type ResolvedTheme = 'light' | 'dark'

const STORAGE_KEY = 'drift.theme.mode'

function loadInitial(): ThemeMode {
  if (typeof window === 'undefined') return 'system'
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    if (stored === 'light' || stored === 'dark' || stored === 'system') return stored
  } catch {
    /* localStorage can throw in private modes / quota; treat as unset */
  }
  return 'system'
}

function persist(mode: ThemeMode): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(STORAGE_KEY, mode)
  } catch {
    /* ignore — preference just won't persist across reloads */
  }
}

function systemPrefersDark(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
  )
}

function resolve(mode: ThemeMode): ResolvedTheme {
  if (mode === 'light' || mode === 'dark') return mode
  return systemPrefersDark() ? 'dark' : 'light'
}

type ThemeStore = {
  mode: ThemeMode
  resolvedMode: ResolvedTheme
  setMode: (mode: ThemeMode) => void
  // Cycles `system → light → dark → system` for a single-button toggle.
  cycleMode: () => void
  // Internal: invoked when the OS prefers-color-scheme flips while the
  // user has `mode === 'system'`. No-op otherwise.
  _onSystemChange: () => void
}

const initialMode = loadInitial()

export const useThemeStore = create<ThemeStore>((set, get) => ({
  mode: initialMode,
  resolvedMode: resolve(initialMode),
  setMode: (mode) => {
    persist(mode)
    set({ mode, resolvedMode: resolve(mode) })
  },
  cycleMode: () => {
    // Order chosen so first click from default `system` lands on the
    // explicit opposite of the current resolved state (system → light
    // when OS is dark feels surprising; system → light → dark gives a
    // predictable two-click path to either explicit mode).
    const order: ThemeMode[] = ['system', 'light', 'dark']
    const next = order[(order.indexOf(get().mode) + 1) % order.length]
    get().setMode(next)
  },
  _onSystemChange: () => {
    if (get().mode !== 'system') return
    set({ resolvedMode: systemPrefersDark() ? 'dark' : 'light' })
  },
}))

// Wire the OS preference listener once at module load. The query stays
// alive for the SPA's lifetime (no cleanup), which is fine — we want
// the page to re-theme any time the OS flips, not just after a
// component mount.
if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  const handler = () => useThemeStore.getState()._onSystemChange()
  // `addEventListener('change', …)` is the modern API; some older
  // Safari versions (< 14) only expose `addListener`. Both shapes
  // share the MediaQueryList type; check at runtime.
  if (typeof mq.addEventListener === 'function') {
    mq.addEventListener('change', handler)
  } else if (typeof (mq as unknown as { addListener?: (h: () => void) => void }).addListener === 'function') {
    ;(mq as unknown as { addListener: (h: () => void) => void }).addListener(handler)
  }
}
