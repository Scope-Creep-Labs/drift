import { useEffect, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Paper,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import KeyIcon from '@mui/icons-material/Key'
import { deployApi, type RegistryCredential } from '../lib/deployApi'

export function RegistryCredsModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [creds, setCreds] = useState<RegistryCredential[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [registry, setRegistry] = useState('')
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const refresh = () => {
    setLoading(true)
    setListError(null)
    deployApi
      .listRegistryCreds()
      .then(setCreds)
      .catch((e: Error) => setListError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    if (open) {
      refresh()
      setRegistry('')
      setUsername('')
      setPassword('')
      setFormError(null)
    }
  }, [open])

  const handleSubmit = async () => {
    if (!registry.trim() || !username.trim() || !password) return
    setSubmitting(true)
    setFormError(null)
    try {
      await deployApi.upsertRegistryCreds(registry.trim(), username.trim(), password)
      // Clear the form, refresh the list. Password stays write-only —
      // we deliberately wipe it after a successful save.
      setRegistry('')
      setUsername('')
      setPassword('')
      refresh()
    } catch (e) {
      setFormError((e as Error).message)
    } finally {
      setSubmitting(false)
    }
  }

  const handleDelete = async (reg: string) => {
    try {
      await deployApi.deleteRegistryCreds(reg)
      refresh()
    } catch (e) {
      setListError((e as Error).message)
    }
  }

  return (
    <Dialog open={open} onClose={submitting ? undefined : onClose} maxWidth="sm" fullWidth>
      <DialogTitle>
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Stack direction="row" alignItems="center" spacing={1}>
            <KeyIcon fontSize="small" />
            <span>Registry credentials</span>
          </Stack>
          <IconButton size="small" onClick={onClose} disabled={submitting}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
      </DialogTitle>

      <DialogContent dividers>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Per-registry pull credentials. Encrypted at rest with{' '}
          <code>DRIFT_SECRET_KEY</code> and delivered to every device's edge agent on the
          next check-in (≤30s).
        </Typography>

        {loading && (
          <Box sx={{ display: 'flex', justifyContent: 'center', py: 4 }}>
            <CircularProgress size={22} />
          </Box>
        )}

        {listError && (
          <Alert severity="error" variant="outlined" sx={{ mb: 2 }}>
            {listError}
          </Alert>
        )}

        {creds !== null && creds.length === 0 && !loading && (
          <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
            No credentials yet. Add one below — the next agent tick will push it to every device.
          </Alert>
        )}

        {(creds ?? []).map((c) => (
          <Paper
            key={c.id}
            variant="outlined"
            sx={{
              p: 1.4,
              mb: 1,
              borderColor: 'divider',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 1,
            }}
          >
            <Box sx={{ minWidth: 0 }}>
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                {c.registry}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
                {c.username} · updated {new Date(c.updated_at).toLocaleString()}
              </Typography>
            </Box>
            <Tooltip title="Delete">
              <IconButton size="small" onClick={() => handleDelete(c.registry)}>
                <DeleteOutlineIcon fontSize="small" />
              </IconButton>
            </Tooltip>
          </Paper>
        ))}

        <Box sx={{ mt: 3, pt: 2, borderTop: 1, borderColor: 'divider' }}>
          <Typography variant="caption" sx={{ display: 'block', mb: 1.4, fontWeight: 600, color: 'text.secondary' }}>
            ADD / REPLACE
          </Typography>
          <Stack spacing={1.2}>
            <TextField
              label="Registry"
              placeholder="ghcr.io"
              size="small"
              value={registry}
              onChange={(e) => setRegistry(e.target.value)}
              disabled={submitting}
              helperText="Match the auths-key in docker config.json (e.g. ghcr.io, docker.io). Re-entering an existing one replaces it."
            />
            <TextField
              label="Username"
              placeholder="kidproquo"
              size="small"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              disabled={submitting}
            />
            <TextField
              label="Password / token"
              placeholder="ghp_…"
              type="password"
              size="small"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              autoComplete="new-password"
              helperText="PAT with read:packages for GHCR. Not stored in browser; sent once to the CP and encrypted there."
            />
          </Stack>
          {formError && (
            <Alert severity="error" variant="outlined" sx={{ mt: 2 }}>
              {formError}
            </Alert>
          )}
        </Box>
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 2 }}>
        <Button onClick={onClose} disabled={submitting} sx={{ textTransform: 'none' }}>
          Close
        </Button>
        <Button
          onClick={handleSubmit}
          disabled={submitting || !registry.trim() || !username.trim() || !password}
          variant="contained"
          disableElevation
          sx={{ textTransform: 'none' }}
          startIcon={submitting ? <CircularProgress size={14} color="inherit" /> : null}
        >
          {submitting ? 'Saving…' : 'Save'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
