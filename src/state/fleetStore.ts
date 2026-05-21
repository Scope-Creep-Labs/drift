import { create } from 'zustand'
import { deployApi, type App, type Device } from '../lib/deployApi'

type FleetStore = {
  devices: Device[]
  apps: App[]
  groups: string[]
  loaded: boolean
  refresh: () => Promise<void>
}

// Holds the three name-lists the prompt-input autocomplete needs:
//   - devices (`@` trigger)
//   - apps (`#` trigger)
//   - groups (`:` trigger), derived from devices' group_id values
// Refreshed on app mount + after every successful conversation turn
// (wired from useInvestigate's SSE 'done' handler) so anything the
// operator just created is autocompleteable on the next prompt.
export const useFleetStore = create<FleetStore>((set) => ({
  devices: [],
  apps: [],
  groups: [],
  loaded: false,

  refresh: async () => {
    try {
      // List of deployments is unused for autocomplete itself, but
      // deviceApi.listDeployments() is the most readily-available
      // group-id surface besides devices.group_id. Devices is the
      // primary source. Three small GETs in parallel.
      const [devices, apps] = await Promise.all([
        deployApi.listDevices().catch(() => [] as Device[]),
        deployApi.listApps().catch(() => [] as App[]),
      ])
      const groups = Array.from(
        new Set(devices.map((d) => d.group_id).filter((g): g is string => !!g)),
      ).sort()
      set({ devices, apps, groups, loaded: true })
    } catch {
      // Best-effort — if the fetch fails, leave existing data. The next
      // refresh (next 'done' event) will try again. No user-visible
      // error: autocomplete just doesn't suggest anything.
    }
  },
}))
