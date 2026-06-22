import { create } from 'zustand'

// Sibling to terminalUiStore — single source of truth for "which device's
// tunnel modal is open". Mounted once at the Shell root so any surface
// can open it (sidebar Tunnel icon, future chat-action card, etc.) and
// closing from anywhere routes back through here.
type TunnelUiStore = {
  openDevice: string | null
  open: (deviceName: string) => void
  close: () => void
}

export const useTunnelUiStore = create<TunnelUiStore>((set) => ({
  openDevice: null,
  open: (deviceName) => set({ openDevice: deviceName }),
  close: () => set({ openDevice: null }),
}))
