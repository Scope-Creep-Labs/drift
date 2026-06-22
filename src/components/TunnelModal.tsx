import { useCallback, useEffect, useState } from 'react'
import {
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  IconButton,
  Link,
  List,
  ListItem,
  ListItemText,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import ContentCopyIcon from '@mui/icons-material/ContentCopy'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import LanIcon from '@mui/icons-material/Lan'
import OpenInNewIcon from '@mui/icons-material/OpenInNew'
import { deployApiBase } from '../lib/apiBase'

const DEPLOY_BASE = deployApiBase()

type TunnelOut = {
  id: string
  device_id: string
  port: number
  status: string
  url: string
  subdomain: string
  created_at: string
  expires_at: string
  ended_at: string | null
}

export function TunnelModal({
  open,
  deviceName,
  onClose,
}: {
  open: boolean
  deviceName: string
  onClose: () => void
}) {
  // Form state
  const [port, setPort] = useState<string>('8080')
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  // Existing-tunnels list state
  const [tunnels, setTunnels] = useState<TunnelOut[]>([])
  const [loadingList, setLoadingList] = useState(false)
  const [listError, setListError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!deviceName) return
    setLoadingList(true)
    setListError(null)
    try {
      const res = await fetch(
        `${DEPLOY_BASE}/devices/${encodeURIComponent(deviceName)}/tunnels`,
        { credentials: 'include' },
      )
      if (!res.ok) {
        const body = await res.text().catch(() => '')
        throw new Error(`${res.status} ${res.statusText}${body ? `: ${body}` : ''}`)
      }
      const rows = (await res.json()) as TunnelOut[]
      setTunnels(rows)
    } catch (e) {
      setListError((e as Error).message)
    } finally {
      setLoadingList(false)
    }
  }, [deviceName])

  useEffect(() => {
    if (!open) return
    void refresh()
    // Poll while open so a freshly attached bridge promotes pending→active
    // without the operator having to refresh. 5s is cheap; the endpoint
    // just lists rows.
    const t = setInterval(() => {
      void refresh()
    }, 5000)
    return () => clearInterval(t)
  }, [open, refresh])

  const onSubmit = async () => {
    setSubmitError(null)
    const portNum = parseInt(port, 10)
    if (!Number.isFinite(portNum) || portNum < 1 || portNum > 65535) {
      setSubmitError('Port must be an integer in 1–65535')
      return
    }
    setSubmitting(true)
    try {
      const res = await fetch(
        `${DEPLOY_BASE}/devices/${encodeURIComponent(deviceName)}/tunnel/open`,
        {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ port: portNum }),
        },
      )
      if (!res.ok) {
        const body = await res.text().catch(() => '')
        throw new Error(`${res.status} ${res.statusText}${body ? `: ${body}` : ''}`)
      }
      await refresh()
    } catch (e) {
      setSubmitError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  const onRevoke = async (id: string) => {
    try {
      const res = await fetch(`${DEPLOY_BASE}/tunnels/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        credentials: 'include',
      })
      // 204 on success; 404 on already-gone — both fine.
      if (!res.ok && res.status !== 404) {
        const body = await res.text().catch(() => '')
        throw new Error(`${res.status} ${res.statusText}${body ? `: ${body}` : ''}`)
      }
      await refresh()
    } catch (e) {
      // Surface inline at the list level — the form error slot is for
      // the open action.
      setListError((e as Error).message)
    }
  }

  const onCopy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // Older browsers / insecure contexts may not allow clipboard
      // writes silently — fall back to a selection-based path could
      // go here, but most users open this UI over HTTPS so just no-op.
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      maxWidth="sm"
      fullWidth
      PaperProps={{ sx: { borderRadius: 1.5 } }}
    >
      <DialogTitle sx={{ pr: 1 }}>
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Stack direction="row" alignItems="center" spacing={1}>
            <LanIcon fontSize="small" />
            <Typography variant="body1" sx={{ fontWeight: 600 }}>
              Tunnel — {deviceName}
            </Typography>
          </Stack>
          <IconButton size="small" onClick={onClose}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
        <Typography variant="caption" color="text.secondary">
          Forward a port on this device's <code>localhost</code> to a randomized HTTPS
          subdomain you can open in your browser. The tunnel expires after 4 hours.
        </Typography>
      </DialogTitle>

      <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1 }}>
        {/* Open form */}
        <Stack direction="row" spacing={1} alignItems="flex-start">
          <TextField
            label="Port"
            size="small"
            value={port}
            onChange={(e) => setPort(e.target.value.replace(/\D/g, '').slice(0, 5))}
            inputProps={{ inputMode: 'numeric', pattern: '[0-9]*' }}
            sx={{ width: 110 }}
            disabled={submitting}
            error={Boolean(submitError)}
            helperText={submitError ?? ' '}
          />
          <Button
            variant="contained"
            size="small"
            onClick={onSubmit}
            disabled={submitting}
            sx={{ height: 40 }}
          >
            {submitting ? <CircularProgress size={16} /> : 'Open tunnel'}
          </Button>
        </Stack>

        {/* Active tunnels list */}
        <Box>
          <Stack direction="row" alignItems="center" justifyContent="space-between">
            <Typography variant="subtitle2">Active tunnels</Typography>
            {loadingList && <CircularProgress size={12} />}
          </Stack>
          {listError && (
            <Typography variant="caption" color="error" sx={{ display: 'block', mt: 0.5 }}>
              {listError}
            </Typography>
          )}
          {!loadingList && tunnels.length === 0 && (
            <Typography variant="caption" color="text.secondary">
              No active tunnels for this device.
            </Typography>
          )}
          <List dense disablePadding>
            {tunnels.map((t) => (
              <ListItem
                key={t.id}
                disableGutters
                sx={{
                  borderTop: 1,
                  borderColor: 'divider',
                  py: 0.5,
                  alignItems: 'flex-start',
                }}
                secondaryAction={
                  <Tooltip title="Revoke tunnel">
                    <IconButton size="small" onClick={() => onRevoke(t.id)}>
                      <DeleteOutlineIcon fontSize="small" />
                    </IconButton>
                  </Tooltip>
                }
              >
                <ListItemText
                  primary={
                    <Stack direction="row" alignItems="center" spacing={1}>
                      <Chip
                        size="small"
                        label={`:${t.port}`}
                        sx={{ height: 18, fontSize: 11 }}
                      />
                      <Chip
                        size="small"
                        variant="outlined"
                        color={t.status === 'active' ? 'success' : 'default'}
                        label={t.status}
                        sx={{ height: 18, fontSize: 10 }}
                      />
                    </Stack>
                  }
                  secondary={
                    <Stack direction="column" sx={{ mt: 0.5 }}>
                      <Stack direction="row" alignItems="center" spacing={0.5}>
                        <Link
                          href={t.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          variant="caption"
                          sx={{
                            fontFamily: 'monospace',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis',
                            whiteSpace: 'nowrap',
                            maxWidth: 320,
                          }}
                        >
                          {t.url}
                        </Link>
                        <Tooltip title="Copy URL">
                          <IconButton size="small" onClick={() => onCopy(t.url)}>
                            <ContentCopyIcon sx={{ fontSize: 12 }} />
                          </IconButton>
                        </Tooltip>
                        <Tooltip title="Open in new tab">
                          <IconButton
                            size="small"
                            component="a"
                            href={t.url}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            <OpenInNewIcon sx={{ fontSize: 12 }} />
                          </IconButton>
                        </Tooltip>
                      </Stack>
                      <Typography variant="caption" color="text.secondary">
                        expires {new Date(t.expires_at).toLocaleString()}
                      </Typography>
                    </Stack>
                  }
                  secondaryTypographyProps={{ component: 'div' }}
                />
              </ListItem>
            ))}
          </List>
        </Box>
      </DialogContent>
    </Dialog>
  )
}
