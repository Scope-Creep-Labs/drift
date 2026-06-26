import { useEffect, useRef, useState } from 'react'
import {
  AppBar,
  Box,
  Drawer,
  IconButton,
  Toolbar,
  Typography,
  useMediaQuery,
  useTheme,
} from '@mui/material'
import MenuIcon from '@mui/icons-material/Menu'
import { InvestigationList } from './Sidebar/InvestigationList'
import { Conversation } from './Conversation'
import { DemoBanner } from './DemoBanner'
import { PromptInput } from './PromptInput'
import { TerminalModal } from './TerminalModal'
import { TunnelModal } from './TunnelModal'
import { TelegramLinkModal } from './TelegramLinkModal'
import { useTerminalUiStore } from '../state/terminalUiStore'
import { useTunnelUiStore } from '../state/tunnelUiStore'
import { useTelegramUiStore } from '../state/telegramUiStore'
import { useInvestigationStore } from '../state/investigationStore'

const SIDEBAR_WIDTH = 260

export function Shell() {
  // Terminal modal is rendered at the App root so any surface (sidebar
  // device row, chat action card) can open it via the shared store.
  const openDevice = useTerminalUiStore((s) => s.openDevice)
  const close = useTerminalUiStore((s) => s.close)
  const openTunnelDevice = useTunnelUiStore((s) => s.openDevice)
  const closeTunnel = useTunnelUiStore((s) => s.close)
  const telegramOpen = useTelegramUiStore((s) => s.open)
  const closeTelegram = useTelegramUiStore((s) => s.close)

  const theme = useTheme()
  // md = 900px. Below this the persistent sidebar collapses into a
  // temporary Drawer triggered by a hamburger in the top AppBar — the
  // standard "doesn't break on phone" pattern.
  const isMobile = useMediaQuery(theme.breakpoints.down('md'))
  const [drawerOpen, setDrawerOpen] = useState(false)

  // Auto-close the mobile drawer when the user picks a conversation —
  // selecting one in the drawer + then having to tap outside to see it
  // would be three steps where one should do. Other interactions (tab
  // changes inside the drawer, terminal-modal opens) leave the drawer
  // alone; user dismisses by tap-outside.
  const activeId = useInvestigationStore((s) => s.activeId)
  const prevActiveId = useRef(activeId)
  useEffect(() => {
    if (activeId !== prevActiveId.current && drawerOpen) {
      setDrawerOpen(false)
    }
    prevActiveId.current = activeId
  }, [activeId, drawerOpen])

  if (!isMobile) {
    // Desktop / tablet landscape: persistent sidebar + main column.
    return (
      <Box sx={{ display: 'flex', flexDirection: 'column', height: '100vh', bgcolor: 'background.default' }}>
        <DemoBanner />
        <Box sx={{ display: 'flex', flex: 1, minHeight: 0 }}>
          <InvestigationList />
          <Box
            sx={{
              flex: 1,
              minWidth: 0,
              display: 'flex',
              flexDirection: 'column',
              minHeight: 0,
            }}
          >
            <Conversation />
            <Box sx={{ px: 4, bgcolor: 'background.default' }}>
              <Box sx={{ maxWidth: 920, mx: 'auto' }}>
                <PromptInput />
              </Box>
            </Box>
          </Box>
        </Box>
        {openDevice && (
          <TerminalModal open={openDevice !== null} deviceName={openDevice} onClose={close} />
        )}
        {openTunnelDevice && (
          <TunnelModal
            open={openTunnelDevice !== null}
            deviceName={openTunnelDevice}
            onClose={closeTunnel}
          />
        )}
        {telegramOpen && (
          <TelegramLinkModal open={telegramOpen} onClose={closeTelegram} />
        )}
      </Box>
    )
  }

  // Mobile: AppBar at top + main column underneath. Sidebar lives in a
  // temporary Drawer that the hamburger toggles. The Drawer dims the
  // main content while open and closes on tap-outside.
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', height: '100vh', bgcolor: 'background.default' }}>
      <DemoBanner />
      <AppBar
        position="static"
        color="default"
        elevation={0}
        sx={{ borderBottom: 1, borderColor: 'divider', bgcolor: 'background.paper' }}
      >
        <Toolbar variant="dense" sx={{ minHeight: 48, gap: 1 }}>
          <IconButton
            edge="start"
            size="small"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open navigation"
          >
            <MenuIcon />
          </IconButton>
          <Typography variant="subtitle1" sx={{ fontWeight: 600, letterSpacing: 0.2 }}>
            Drift
          </Typography>
        </Toolbar>
      </AppBar>

      <Drawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        // ModalProps.keepMounted=true preserves the sidebar's scroll
        // position + any in-progress filter input across opens — feels
        // less jarring than re-mounting the whole list on every toggle.
        ModalProps={{ keepMounted: true }}
        PaperProps={{ sx: { width: SIDEBAR_WIDTH } }}
      >
        {/* InvestigationList sets its own height: 100vh which is fine
            inside the Drawer paper (Drawer pins its paper to 100% height
            anyway). Auto-close on activeId change is handled by the
            useEffect above — clicking the filter input or tabs here
            mustn't close the drawer. */}
        <InvestigationList />
      </Drawer>

      <Box
        sx={{
          flex: 1,
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0,
        }}
      >
        <Conversation />
        <Box sx={{ px: 2, pb: 1, bgcolor: 'background.default' }}>
          <PromptInput />
        </Box>
      </Box>
      {openDevice && (
        <TerminalModal open={openDevice !== null} deviceName={openDevice} onClose={close} />
      )}
      {openTunnelDevice && (
        <TunnelModal
          open={openTunnelDevice !== null}
          deviceName={openTunnelDevice}
          onClose={closeTunnel}
        />
      )}
      {telegramOpen && (
        <TelegramLinkModal open={telegramOpen} onClose={closeTelegram} />
      )}
    </Box>
  )
}
