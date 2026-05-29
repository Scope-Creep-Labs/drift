import { createTheme, type ThemeOptions } from '@mui/material/styles'

// Shared bits between light and dark — typography, radii, component
// defaults. Only the palette differs.
const sharedTypography: ThemeOptions['typography'] = {
  fontFamily:
    '"Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif',
  fontSize: 15,
  h6: { fontWeight: 600, fontSize: '0.95rem' },
  body2: { lineHeight: 1.55 },
}

const sharedComponents: ThemeOptions['components'] = {
  MuiPaper: {
    styleOverrides: {
      // MUI's default Paper overlays a translucent white gradient on
      // higher elevations in dark mode — kills the flat aesthetic. Same
      // override is fine in light mode (the gradient is invisible there).
      root: { backgroundImage: 'none' },
    },
  },
  MuiButton: {
    defaultProps: { disableElevation: true },
    styleOverrides: { root: { textTransform: 'none', fontWeight: 500 } },
  },
}

export const darkTheme = createTheme({
  palette: {
    mode: 'dark',
    primary: { main: '#7c9cff' },
    secondary: { main: '#5ad1c1' },
    background: {
      default: '#0e1116',
      paper: '#161b22',
    },
    divider: 'rgba(255,255,255,0.08)',
  },
  typography: sharedTypography,
  shape: { borderRadius: 8 },
  components: sharedComponents,
})

export const lightTheme = createTheme({
  palette: {
    mode: 'light',
    // Slightly darker primary than the dark-mode variant so contrast
    // against a white background hits AA. The hue is the same family
    // (cornflower-ish), just shifted darker.
    primary: { main: '#3b5bd1' },
    secondary: { main: '#1f8a78' },
    background: {
      // Subtle off-white for the page background so panels stand out;
      // pure white for Paper surfaces gives a clear elevation read.
      default: '#f6f8fa',
      paper: '#ffffff',
    },
    divider: 'rgba(0,0,0,0.10)',
  },
  typography: sharedTypography,
  shape: { borderRadius: 8 },
  components: sharedComponents,
})

// Legacy alias kept so callers that haven't migrated to the
// store-driven runtime resolution still compile. New code should
// import `darkTheme` / `lightTheme` directly and read the active
// theme from `useThemeStore`.
export const theme = darkTheme
