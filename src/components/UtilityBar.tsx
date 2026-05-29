import { IconButton, Stack, Tooltip } from '@mui/material'
import DarkModeIcon from '@mui/icons-material/DarkModeOutlined'
import LightModeIcon from '@mui/icons-material/LightModeOutlined'
import SettingsBrightnessIcon from '@mui/icons-material/SettingsBrightnessOutlined'
import { useAuth } from '../auth/AuthContext'
import { useThemeStore } from '../state/themeStore'

// Small floating utility row pinned to the top-right of the viewport.
// Holds the theme cycle ONLY — admin and per-user settings (LLM config,
// software updates, registry creds, password change, sign-out) all live
// in the sidebar footer, where they're discoverable alongside the user
// identity. Keeping the floating bar minimal avoids competing focus
// targets in the visual hierarchy.
export function UtilityBar() {
  const themeMode = useThemeStore((s) => s.mode)
  const cycleTheme = useThemeStore((s) => s.cycleMode)
  const auth = useAuth()

  // Don't render on the login screen.
  if (auth.status !== 'authenticated') return null

  return (
    <Stack
      direction="row"
      spacing={0.5}
      sx={{
        position: 'fixed',
        top: 8,
        right: 12,
        zIndex: (t) => t.zIndex.appBar + 1,
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
  )
}
