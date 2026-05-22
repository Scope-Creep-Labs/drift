import { Box } from '@mui/material'
import { InvestigationList } from './Sidebar/InvestigationList'
import { Conversation } from './Conversation'
import { PromptInput } from './PromptInput'
import { TerminalModal } from './TerminalModal'
import { useTerminalUiStore } from '../state/terminalUiStore'

export function Shell() {
  // The terminal modal is rendered at the App root so any surface can
  // open it via the shared Zustand store (sidebar device row,
  // TerminalActionBlock card in chat, future LLM-driven triggers).
  const openDevice = useTerminalUiStore((s) => s.openDevice)
  const close = useTerminalUiStore((s) => s.close)

  return (
    <Box sx={{ display: 'flex', height: '100vh', bgcolor: 'background.default' }}>
      <InvestigationList />
      <Box
        sx={{
          flex: 1,
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          height: '100vh',
        }}
      >
        <Conversation />
        <Box sx={{ px: 4, bgcolor: 'background.default' }}>
          <Box sx={{ maxWidth: 920, mx: 'auto' }}>
            <PromptInput />
          </Box>
        </Box>
      </Box>
      {openDevice && (
        <TerminalModal open={openDevice !== null} deviceName={openDevice} onClose={close} />
      )}
    </Box>
  )
}
