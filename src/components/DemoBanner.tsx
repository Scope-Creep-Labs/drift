import { Alert, AlertTitle, Box } from '@mui/material'
import ScienceIcon from '@mui/icons-material/Science'
import { useAuth } from '../auth/AuthContext'

/**
 * Sticky info banner shown when the CP is running in DEMO_MODE.
 *
 * Drives off `auth.user.demo_mode` (server-supplied on every /me
 * response). The banner sits at the top of the viewport above the
 * Shell so the user immediately knows they're in a shared environment
 * before they start mutating filters / pushing app revisions etc.
 *
 * Renders nothing when DEMO_MODE is off — zero overhead on
 * non-demo deploys.
 */
export function DemoBanner() {
  const auth = useAuth()
  if (auth.status !== 'authenticated') return null
  if (!auth.user.demo_mode) return null

  // server-supplied banner text wins; fall back to a sensible default
  // if for some reason it's missing.
  const message =
    auth.user.demo_banner_message ??
    'Demo mode — actions are visible to other operators on this account. State resets nightly.'

  return (
    <Box sx={{ flexShrink: 0 }}>
      <Alert
        severity="info"
        variant="filled"
        icon={<ScienceIcon fontSize="small" />}
        sx={{
          borderRadius: 0,
          py: 0.4,
          alignItems: 'center',
          '& .MuiAlert-icon': { mr: 1, py: 0 },
          '& .MuiAlert-message': { py: 0, fontSize: '0.82rem' },
        }}
      >
        <AlertTitle sx={{ m: 0, fontSize: '0.82rem', fontWeight: 600, display: 'inline', mr: 1 }}>
          Demo
        </AlertTitle>
        {message}
      </Alert>
    </Box>
  )
}
