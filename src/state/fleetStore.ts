import { create } from 'zustand'
import { deployApi, type App, type Device, type DeploymentTarget } from '../lib/deployApi'

type FleetStore = {
  devices: Device[]
  apps: App[]
  groups: string[]
  deployments: DeploymentTarget[]
  loaded: boolean
  refresh: () => Promise<void>
}

// Single source of truth for fleet metadata in the SPA. Used by:
//   - prompt-input autocomplete (@device, #app, :group)
//   - sidebar Apps section (per-app status rollup + deployment counts)
// Refreshed on auth-mount + after every successful conversation turn
// (wired from useInvestigate's SSE 'done' handler) so a deploy/create/
// delete done via the chat is visible to UI consumers on the very next
// render — no page reload needed.
export const useFleetStore = create<FleetStore>((set) => ({
  devices: [],
  apps: [],
  groups: [],
  deployments: [],
  loaded: false,

  refresh: async () => {
    try {
      // Three parallel small GETs. Each catches its own error so a
      // single failing endpoint doesn't blank the whole store.
      const [devices, apps, deployments] = await Promise.all([
        deployApi.listDevices().catch(() => [] as Device[]),
        deployApi.listApps().catch(() => [] as App[]),
        deployApi.listDeployments().catch(() => [] as DeploymentTarget[]),
      ])
      const groups = Array.from(
        new Set(devices.map((d) => d.group_id).filter((g): g is string => !!g)),
      ).sort()
      set({ devices, apps, groups, deployments, loaded: true })
    } catch {
      // Best-effort — if all fetches fail, leave existing data alone.
      // Next refresh will try again.
    }
  },
}))
