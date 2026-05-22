import { Box, Button, Stack, Typography } from '@mui/material'
import TerminalIcon from '@mui/icons-material/Terminal'
import OpenInFullIcon from '@mui/icons-material/OpenInFull'
import type { TerminalActionBlock as TerminalActionBlockT } from '../../types/blocks'
import { useTerminalUiStore } from '../../state/terminalUiStore'
import { useFleetStore } from '../../state/fleetStore'
import { useAuth, hasGroup, isDeploy } from '../../auth/AuthContext'

// Renders an "Open terminal to X" card the agent emits when the user
// asks for / would benefit from interactive host access. The card
// stays in the chat as an audit trail; clicking the button drives the
// same store the sidebar Devices row uses.
export function TerminalActionBlock({ block }: { block: TerminalActionBlockT }) {
  const open = useTerminalUiStore((s) => s.open)
  const devices = useFleetStore((s) => s.devices)
  const auth = useAuth()

  // Resolve the device the agent referenced. We tolerate it being
  // absent from the local fleet snapshot (just-deleted, race with a
  // refresh) — the button stays disabled with an explanation.
  const device = devices.find((d) => d.name === block.device_name)
  const user = auth.status === 'authenticated' ? auth.user : undefined
  const canOpen =
    !!device &&
    device.status === 'online' &&
    isDeploy(user) &&
    (user?.role === 'admin' || hasGroup(user, device.group_id))

  let disabledReason: string | null = null
  if (!device) disabledReason = 'device not found in your current view'
  else if (device.status !== 'online') disabledReason = `device is ${device.status}`
  else if (!isDeploy(user)) disabledReason = 'requires the deploy role'
  else if (user?.role !== 'admin' && !hasGroup(user, device.group_id))
    disabledReason = `requires membership in group '${device.group_id ?? '<none>'}'`

  return (
    <Box
      sx={{
        p: 1.5,
        border: 1,
        borderColor: 'divider',
        borderRadius: 1,
        bgcolor: 'rgba(255,255,255,0.02)',
        display: 'flex',
        alignItems: 'center',
        gap: 1.5,
      }}
    >
      <TerminalIcon sx={{ color: 'text.secondary' }} />
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Stack direction="row" alignItems="center" spacing={1} sx={{ minWidth: 0 }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            Open terminal to {block.device_name}
          </Typography>
          {device && (
            <Box
              sx={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                bgcolor:
                  device.status === 'online'
                    ? 'success.main'
                    : device.status === 'offline'
                      ? 'error.main'
                      : 'warning.main',
              }}
            />
          )}
        </Stack>
        {block.reason && (
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
            {block.reason}
          </Typography>
        )}
        {disabledReason && (
          <Typography variant="caption" color="text.disabled" sx={{ display: 'block' }}>
            {disabledReason}
          </Typography>
        )}
      </Box>
      <Button
        size="small"
        variant="contained"
        disableElevation
        startIcon={<OpenInFullIcon fontSize="small" />}
        onClick={() => open(block.device_name)}
        disabled={!canOpen}
        sx={{ textTransform: 'none', flexShrink: 0 }}
      >
        Open
      </Button>
    </Box>
  )
}
