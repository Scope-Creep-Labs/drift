import { create } from 'zustand'

// Per-browser-tab session record of which live_chart `chart_key`s have
// been emitted by the agent in this session. NOT persisted — on a page
// reload this starts empty, so any live_chart block rehydrated from
// localStorage mounts in the paused state (LiveChartBlock reads this).
//
// The agent re-emitting the same chart_key (replace-in-place mutation)
// also bumps the timestamp here, which the LiveChartBlock useEffect
// watches → auto-resume on the latest version.
type LiveChartSession = {
  emissions: Record<string, number>
  markEmitted: (chartKey: string) => void
}

export const useLiveChartSession = create<LiveChartSession>((set) => ({
  emissions: {},
  markEmitted: (chartKey) =>
    set((s) => ({ emissions: { ...s.emissions, [chartKey]: Date.now() } })),
}))
