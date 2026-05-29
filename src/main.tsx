import React from 'react'
import ReactDOM from 'react-dom/client'
import { ThemeProvider } from '@mui/material/styles'
import CssBaseline from '@mui/material/CssBaseline'
import { QueryClientProvider } from '@tanstack/react-query'
import App from './App'
import { AuthProvider } from './auth/AuthContext'
import { darkTheme, lightTheme } from './theme'
import { useThemeStore } from './state/themeStore'
import { queryClient } from './query/client'

// Reads the resolved theme from the store and feeds it to MUI's
// ThemeProvider. The store seeds itself from localStorage at load and
// subscribes once to `prefers-color-scheme` so an OS flip rethemes the
// app instantly when the user has selected `system` mode.
function ThemedApp() {
  const resolvedMode = useThemeStore((s) => s.resolvedMode)
  const theme = resolvedMode === 'light' ? lightTheme : darkTheme
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <AuthProvider>
        <App />
      </AuthProvider>
    </ThemeProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemedApp />
    </QueryClientProvider>
  </React.StrictMode>,
)
