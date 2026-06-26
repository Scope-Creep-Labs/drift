import { create } from 'zustand'

// Single-instance state for "is the Telegram link modal open" — sibling
// to terminalUiStore + tunnelUiStore. Lets any surface (sidebar settings
// icon, future chat-action card) open the modal through one path.
type TelegramUiStore = {
  open: boolean
  show: () => void
  close: () => void
}

export const useTelegramUiStore = create<TelegramUiStore>((set) => ({
  open: false,
  show: () => set({ open: true }),
  close: () => set({ open: false }),
}))
