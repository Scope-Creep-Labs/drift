import { FormEvent, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  TextField,
  Typography,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import LockResetIcon from '@mui/icons-material/LockReset'
import { useAuth } from '../auth/AuthContext'

const MIN_LENGTH = 8

export function ChangePasswordModal({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const { changePassword } = useAuth()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState(false)

  // Reset state every time the modal opens — credentials shouldn't
  // sit in component memory between sessions.
  useEffect(() => {
    if (open) {
      setCurrent('')
      setNext('')
      setConfirm('')
      setError(null)
      setSuccess(false)
      setSubmitting(false)
    }
  }, [open])

  const newMismatch = next.length > 0 && confirm.length > 0 && next !== confirm
  const newTooShort = next.length > 0 && next.length < MIN_LENGTH
  const sameAsCurrent = next.length > 0 && current.length > 0 && next === current
  const canSubmit =
    !submitting &&
    current.length > 0 &&
    next.length >= MIN_LENGTH &&
    next === confirm &&
    next !== current

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!canSubmit) return
    setSubmitting(true)
    setError(null)
    try {
      await changePassword(current, next)
      setSuccess(true)
      // Wipe input fields immediately. Close after a brief delay so the
      // success message is visible.
      setCurrent('')
      setNext('')
      setConfirm('')
      setTimeout(() => onClose(), 1200)
    } catch (e) {
      setError((e as Error).message)
      setSubmitting(false)
    }
  }

  return (
    <Dialog open={open} onClose={submitting ? undefined : onClose} maxWidth="xs" fullWidth>
      <DialogTitle>
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Stack direction="row" alignItems="center" spacing={1}>
            <LockResetIcon fontSize="small" />
            <span>Change password</span>
          </Stack>
          <IconButton size="small" onClick={onClose} disabled={submitting}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
      </DialogTitle>

      <form onSubmit={onSubmit}>
        <DialogContent dividers>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            Updating your own password — you'll stay signed in afterwards. Minimum {MIN_LENGTH} characters.
          </Typography>

          <Stack spacing={1.5}>
            <TextField
              label="Current password"
              type="password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              size="small"
              autoFocus
              autoComplete="current-password"
              disabled={submitting || success}
              fullWidth
            />
            <TextField
              label="New password"
              type="password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              size="small"
              autoComplete="new-password"
              disabled={submitting || success}
              fullWidth
              error={newTooShort || sameAsCurrent}
              helperText={
                newTooShort
                  ? `At least ${MIN_LENGTH} characters`
                  : sameAsCurrent
                    ? 'Must be different from current password'
                    : ' '
              }
            />
            <TextField
              label="Confirm new password"
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              size="small"
              autoComplete="new-password"
              disabled={submitting || success}
              fullWidth
              error={newMismatch}
              helperText={newMismatch ? "Doesn't match" : ' '}
            />
          </Stack>

          {error && (
            <Alert severity="error" variant="outlined" sx={{ mt: 2 }}>
              {error}
            </Alert>
          )}
          {success && (
            <Alert severity="success" variant="outlined" sx={{ mt: 2 }}>
              Password updated. Use the new one next time you sign in.
            </Alert>
          )}
        </DialogContent>

        <DialogActions sx={{ px: 3, py: 2 }}>
          <Button onClick={onClose} disabled={submitting} sx={{ textTransform: 'none' }}>
            Cancel
          </Button>
          <Button
            type="submit"
            disabled={!canSubmit || success}
            variant="contained"
            disableElevation
            sx={{ textTransform: 'none' }}
            startIcon={submitting ? <CircularProgress size={14} color="inherit" /> : null}
          >
            {submitting ? 'Updating…' : 'Update password'}
          </Button>
        </DialogActions>
      </form>
    </Dialog>
  )
}
