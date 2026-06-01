import { useEffect, useState } from 'react'
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
  Paper,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import FilterAltIcon from '@mui/icons-material/FilterAlt'
import PublicIcon from '@mui/icons-material/Public'
import LockIcon from '@mui/icons-material/Lock'
import { filtersApi, type OperatorFilterRow } from '../lib/filtersApi'

export function FiltersModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [filters, setFilters] = useState<OperatorFilterRow[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [listError, setListError] = useState<string | null>(null)
  const [pendingId, setPendingId] = useState<string | null>(null)

  const refresh = () => {
    setLoading(true)
    setListError(null)
    filtersApi
      .list()
      .then(setFilters)
      .catch((e: Error) => setListError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    if (open) refresh()
  }, [open])

  const handleDelete = async (id: string) => {
    setPendingId(id)
    try {
      await filtersApi.delete(id)
      refresh()
    } catch (e) {
      setListError((e as Error).message)
    } finally {
      setPendingId(null)
    }
  }

  const handlePromote = async (id: string) => {
    setPendingId(id)
    try {
      await filtersApi.promote(id)
      refresh()
    } catch (e) {
      setListError((e as Error).message)
    } finally {
      setPendingId(null)
    }
  }

  // Sort: fleet first (shared > private), then by created_at desc within
  // each bucket. Surfaces shared rules at the top of the list since
  // they're more "load-bearing" — accidentally adding a duplicate of
  // one is the most common source of clutter.
  const sorted = (filters ?? []).slice().sort((a, b) => {
    if (a.visibility !== b.visibility) {
      return a.visibility === 'fleet' ? -1 : 1
    }
    return b.created_at.localeCompare(a.created_at)
  })

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>
        <Stack direction="row" alignItems="center" justifyContent="space-between">
          <Stack direction="row" alignItems="center" spacing={1}>
            <FilterAltIcon fontSize="small" />
            <span>Noise filters</span>
          </Stack>
          <IconButton size="small" onClick={onClose}>
            <CloseIcon fontSize="small" />
          </IconButton>
        </Stack>
      </DialogTitle>

      <DialogContent dividers>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Rules the investigation agent applies to suppress recurring noise from its
          summaries. Add new ones by telling the agent in chat ("ignore the cadvisor
          product_name error on the pi"). Private filters apply only to your own
          investigations; promote one to <strong>fleet</strong> to share it with every
          operator. Only the creator can delete a filter.
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

        {filters !== null && filters.length === 0 && !loading && (
          <Alert severity="info" variant="outlined" sx={{ mb: 2 }}>
            No filters yet. Tell the agent "ignore X on device Y" in chat to add one.
          </Alert>
        )}

        {sorted.map((f) => {
          const isFleet = f.visibility === 'fleet'
          const scopeKeys: Array<keyof OperatorFilterRow['scope']> = [
            'device',
            'container',
            'group',
            'signal',
          ]
          const scopeChips = scopeKeys
            .map((k) => ({ k, v: f.scope?.[k] }))
            .filter((s) => s.v)
          return (
            <Paper
              key={f.id}
              variant="outlined"
              sx={{
                p: 1.4,
                mb: 1,
                borderColor: 'divider',
                display: 'flex',
                alignItems: 'flex-start',
                justifyContent: 'space-between',
                gap: 1,
              }}
            >
              <Box sx={{ minWidth: 0, flex: 1 }}>
                <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
                  <Tooltip title={isFleet ? 'Fleet-wide: visible to every operator' : 'Private: visible only to you'}>
                    <Chip
                      size="small"
                      label={isFleet ? 'fleet' : 'private'}
                      color={isFleet ? 'primary' : 'default'}
                      icon={isFleet ? <PublicIcon sx={{ fontSize: 14 }} /> : <LockIcon sx={{ fontSize: 14 }} />}
                      sx={{ fontWeight: 600 }}
                    />
                  </Tooltip>
                  {scopeChips.length === 0 ? (
                    <Chip size="small" label="any source" variant="outlined" />
                  ) : (
                    scopeChips.map((s) => (
                      <Chip
                        key={s.k}
                        size="small"
                        variant="outlined"
                        label={`${s.k}=${s.v}`}
                      />
                    ))
                  )}
                </Stack>
                <Typography
                  variant="body2"
                  sx={{
                    fontFamily: 'monospace',
                    fontSize: '0.82rem',
                    wordBreak: 'break-word',
                    mb: 0.4,
                  }}
                >
                  {f.pattern}
                </Typography>
                {f.reason && (
                  <Typography
                    variant="caption"
                    color="text.secondary"
                    sx={{ display: 'block', fontStyle: 'italic', mb: 0.2 }}
                  >
                    "{f.reason}"
                  </Typography>
                )}
                <Typography variant="caption" color="text.secondary">
                  applied {f.apply_count}× · created {new Date(f.created_at).toLocaleDateString()}
                  {f.last_applied_at
                    ? ` · last ${new Date(f.last_applied_at).toLocaleDateString()}`
                    : ''}
                </Typography>
              </Box>
              <Stack direction="row" spacing={0.4} sx={{ flexShrink: 0 }}>
                {!isFleet && f.owned_by_me && (
                  <Tooltip title="Promote to fleet-wide">
                    <span>
                      <IconButton
                        size="small"
                        onClick={() => handlePromote(f.id)}
                        disabled={pendingId === f.id}
                      >
                        <PublicIcon fontSize="small" />
                      </IconButton>
                    </span>
                  </Tooltip>
                )}
                {f.owned_by_me ? (
                  <Tooltip title="Delete">
                    <span>
                      <IconButton
                        size="small"
                        onClick={() => handleDelete(f.id)}
                        disabled={pendingId === f.id}
                      >
                        <DeleteOutlineIcon fontSize="small" />
                      </IconButton>
                    </span>
                  </Tooltip>
                ) : (
                  <Tooltip title="Only the original creator can delete this fleet filter">
                    <span>
                      <IconButton size="small" disabled>
                        <DeleteOutlineIcon fontSize="small" />
                      </IconButton>
                    </span>
                  </Tooltip>
                )}
              </Stack>
            </Paper>
          )
        })}
      </DialogContent>

      <DialogActions sx={{ px: 3, py: 2 }}>
        <Button onClick={onClose} sx={{ textTransform: 'none' }}>
          Close
        </Button>
      </DialogActions>
    </Dialog>
  )
}
