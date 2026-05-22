import { create } from 'zustand'

// Tiny global store for "which device's terminal modal should be open
// right now". Multiple surfaces can request a terminal:
//   - sidebar Devices row click
//   - chat-emitted TerminalActionBlock card click
// Both go through this store so there's exactly one modal mount in
// the React tree (rendered at the App root) and clicking from either
// surface is consistent.
type TerminalUiStore = {
  openDevice: string | null
  open: (deviceName: string) => void
  close: () => void
}

export const useTerminalUiStore = create<TerminalUiStore>((set) => ({
  openDevice: null,
  open: (deviceName) => set({ openDevice: deviceName }),
  close: () => set({ openDevice: null }),
}))
