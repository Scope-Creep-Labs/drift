import { Box, CircularProgress } from '@mui/material'
import { useAuth } from './auth/AuthContext'
import { Shell } from './components/Shell'
import { LoginPage } from './components/LoginPage'
import { UtilityBar } from './components/UtilityBar'

export default function App() {
  const auth = useAuth()
  if (auth.status === 'loading') {
    return (
      <Box
        sx={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          bgcolor: 'background.default',
        }}
      >
        <CircularProgress size={24} />
      </Box>
    )
  }
  if (auth.status === 'unauthenticated') {
    return <LoginPage />
  }
  return (
    <>
      <Shell />
      <UtilityBar />
    </>
  )
}
