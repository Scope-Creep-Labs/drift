import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Box,
  Button,
  Chip,
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
import { deployApi, type Device } from '../lib/deployApi'

// Normalize tag client-side to match server behavior (lowercase +
// strip). Keeps the chip preview consistent before save.
function normalize(s: string): string {
  return s.trim().toLowerCase()
}

export function TagEditModal({
  open,
  device,
  onClose,
  onSaved,
}: {
  open: boolean
  device: Device | null
  onClose: () => void
  onSaved: (updated: Device) => void
}) {
  // Local working copy of the tag list — mutated through chip
  // delete / input add. Reset whenever the modal opens for a new
  // device so a previous-session draft doesn't leak.
  const [tags, setTags] = useState<string[]>([])
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (open && device) {
      setTags(device.tags ?? [])
      setDraft('')
      setError(null)
    }
  }, [open, device])

  const original = useMemo(() => new Set(device?.tags ?? []), [device])
  const dirty = useMemo(() => {
    if (!device) return false
    const now = new Set(tags)
    if (now.size !== original.size) return true
    for (const t of now) if (!original.has(t)) return true
    return false
  }, [device, original, tags])

  const addTag = () => {
    const t = normalize(draft)
    setDraft('')
    if (!t) return
    if (tags.includes(t)) return
    setTags([...tags, t])
  }

  const removeTag = (t: string) => setTags(tags.filter((x) => x !== t))

  const save = async () => {
    if (!device) return
    setSaving(true)
    setError(null)
    try {
      // `set` replaces the full list on the server — single round
      // trip rather than computing add/remove deltas client-side.
      const updated = await deployApi.patchDeviceTags(device.name, { set: tags })
      onSaved(updated)
      onClose()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'failed to save tags')
    } finally {
      setSaving(false)
    }
  }

  if (!device) return null

  return (
    <Dialog open={open} onClose={onClose} maxWidth="xs" fullWidth>
      <DialogTitle sx={{ pb: 1, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Stack>
          <Typography variant="body1" fontWeight={600}>Tags</Typography>
          <Typography variant="caption" color="text.secondary" sx={{ fontFamily: 'monospace' }}>
            {device.name}
          </Typography>
        </Stack>
        <IconButton size="small" onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {error && <Alert severity="error" sx={{ mb: 1.5 }}>{error}</Alert>}
        <Box sx={{ mb: 1.5, minHeight: 36, display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
          {tags.length === 0 ? (
            <Typography variant="caption" color="text.secondary">(no tags)</Typography>
          ) : (
            tags.map((t) => (
              <Chip
                key={t}
                size="small"
                label={t}
                onDelete={() => removeTag(t)}
                sx={{ fontFamily: 'monospace', fontSize: '0.72rem' }}
              />
            ))
          )}
        </Box>
        <Stack direction="row" spacing={1}>
          <TextField
            size="small"
            fullWidth
            placeholder="add tag (e.g. edge, client-z, production)"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ',') {
                e.preventDefault()
                addTag()
              }
            }}
            inputProps={{ style: { fontSize: '0.85rem' } }}
          />
          <Button size="small" variant="outlined" onClick={addTag} disabled={!draft.trim()}>
            Add
          </Button>
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1 }}>
          Tags are case-insensitive and stripped. Press Enter or comma to add.
        </Typography>
        {device.group_id && (
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
            Group <code style={{ fontFamily: 'monospace' }}>{device.group_id}</code> is separate
            from tags — it controls access (admins scoped to groups) and isn't affected by edits here.
          </Typography>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} size="small" disabled={saving}>Cancel</Button>
        <Button
          variant="contained"
          size="small"
          onClick={save}
          disabled={!dirty || saving}
          startIcon={saving ? <CircularProgress size={14} /> : undefined}
        >
          {saving ? 'Saving…' : 'Save'}
        </Button>
      </DialogActions>
    </Dialog>
  )
}
