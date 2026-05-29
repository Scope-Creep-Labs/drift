import { useState } from 'react'
import { IconButton, Stack, Tooltip } from '@mui/material'
import DarkModeIcon from '@mui/icons-material/DarkModeOutlined'
import LightModeIcon from '@mui/icons-material/LightModeOutlined'
import SettingsBrightnessIcon from '@mui/icons-material/SettingsBrightnessOutlined'
import TuneIcon from '@mui/icons-material/TuneOutlined'
import { useAuth } from '../auth/AuthContext'
import { useThemeStore } from '../state/themeStore'
import { LlmSettingsModal } from './LlmSettingsModal'

// Small floating utility row pinned to the top-right of the viewport.
// Holds the theme cycle and (for admins) a settings cog that opens the
// LLM model + API key modal. Sits above all content with a high z-index
// so it stays visible regardless of which surface is rendered below.
//
// Mobile note: the mobile layout already has its own AppBar with a
// hamburger; rather than duplicate this strip there, we hide it under
// the breakpoint. The AppBar absorbs the settings entry point on
// mobile via a second pass once we know we need it.
export function UtilityBar() {
  const themeMode = useThemeStore((s) => s.mode)
  const cycleTheme = useThemeStore((s) => s.cycleMode)
  const auth = useAuth()
  const user = auth.status === 'authenticated' ? auth.user : null
  const [llmOpen, setLlmOpen] = useState(false)

  // Don't render on the login screen — it's clutter when the operator
  // hasn't authenticated yet. Theme + admin settings only matter once
  // we know who's looking at the SPA.
  if (auth.status !== 'authenticated') return null

  return (
    <>
      <Stack
        direction="row"
        spacing={0.5}
        sx={{
          position: 'fixed',
          top: 8,
          right: 12,
          zIndex: (t) => t.zIndex.appBar + 1,
          // Translucent backdrop so the icons read against both bright
          // (light theme) and dark page backgrounds equally well, and
          // so the bar floats over scrolled content without bleeding
          // into the underlying surface.
          bgcolor: (t) =>
            t.palette.mode === 'dark'
              ? 'rgba(22,27,34,0.85)'
              : 'rgba(255,255,255,0.85)',
          backdropFilter: 'blur(6px)',
          border: 1,
          borderColor: 'divider',
          borderRadius: 999,
          px: 0.5,
          py: 0.25,
          alignItems: 'center',
        }}
      >
        {user?.role === 'admin' && (
          <Tooltip title="LLM model + API key">
            <IconButton size="small" onClick={() => setLlmOpen(true)} sx={{ p: 0.6 }}>
              <TuneIcon sx={{ fontSize: 16 }} />
            </IconButton>
          </Tooltip>
        )}
        <Tooltip
          title={
            themeMode === 'system'
              ? 'Theme: system (auto). Click for light.'
              : themeMode === 'light'
                ? 'Theme: light. Click for dark.'
                : 'Theme: dark. Click for system.'
          }
        >
          <IconButton size="small" onClick={cycleTheme} sx={{ p: 0.6 }}>
            {themeMode === 'system' ? (
              <SettingsBrightnessIcon sx={{ fontSize: 16 }} />
            ) : themeMode === 'light' ? (
              <LightModeIcon sx={{ fontSize: 16 }} />
            ) : (
              <DarkModeIcon sx={{ fontSize: 16 }} />
            )}
          </IconButton>
        </Tooltip>
      </Stack>

      {user?.role === 'admin' && (
        <LlmSettingsModal open={llmOpen} onClose={() => setLlmOpen(false)} />
      )}
    </>
  )
}
