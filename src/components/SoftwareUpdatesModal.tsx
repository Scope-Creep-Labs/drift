import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
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
  Link,
  Stack,
  Typography,
} from '@mui/material'
import CloseIcon from '@mui/icons-material/Close'
import RefreshIcon from '@mui/icons-material/Refresh'
import UpdateIcon from '@mui/icons-material/Update'
import CheckCircleIcon from '@mui/icons-material/CheckCircle'
import NewReleasesIcon from '@mui/icons-material/NewReleases'
import ExpandMoreIcon from '@mui/icons-material/ExpandMore'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { apiBase } from '../lib/apiBase'

// Shape of GET /api/admin/updates — keep in sync with
// drift-agent/app/admin/updates.py:get_snapshot().
type ImageStatus = {
  name: string
  image: string
  tag: string
  description: string
  current_digest: string | null
  available_digest: string | null
  update_available: boolean
  last_check: string | null
  error: string | null
}

type ReleaseNote = {
  tag: string
  name: string
  body: string
  html_url: string
  published_at: string
}

type Snapshot = {
  checked_at: string | null
  install_version: string | null
  latest_release_tag: string | null
  bundle_update_available: boolean
  images: ImageStatus[]
  edge_agent: {
    version: string | null
    sha: string | null
    note: string
  }
  releases: ReleaseNote[]
}

export function SoftwareUpdatesModal({
  open,
  onClose,
}: {
  open: boolean
  onClose: () => void
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [loading, setLoading] = useState(false)
  const [applying, setApplying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [applyResult, setApplyResult] = useState<string | null>(null)
  // drift-frontend's digest when the modal first loaded a snapshot.
  // If the LIVE digest changes (operator hit Update now and the recreate
  // finished), the user's currently-loaded SPA bundle is stale and the
  // sensible next action is `window.location.reload()`, not another
  // Update click. We swap the primary button accordingly.
  const [initialFrontendDigest, setInitialFrontendDigest] = useState<string | null>(null)
  const liveFrontendDigest = useMemo(
    () => snapshot?.images.find((i) => i.name === 'drift-frontend')?.current_digest ?? null,
    [snapshot],
  )
  const needsRefresh =
    initialFrontendDigest != null &&
    liveFrontendDigest != null &&
    initialFrontendDigest !== liveFrontendDigest

  const refresh = useCallback(async (force = false) => {
    setLoading(true)
    setError(null)
    try {
      const url = force ? `${apiBase()}/admin/updates/check` : `${apiBase()}/admin/updates`
      const res = await fetch(url, {
        method: force ? 'POST' : 'GET',
        credentials: 'include',
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data: Snapshot = await res.json()
      setSnapshot(data)
      // Remember the first drift-frontend digest we saw, so a later
      // change implies the loaded SPA bundle has gone stale.
      setInitialFrontendDigest((prev) => {
        if (prev != null) return prev
        const d = data.images.find((i) => i.name === 'drift-frontend')?.current_digest
        return d ?? null
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'failed to load')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (open) refresh(false)
  }, [open, refresh])

  const apply = useCallback(async () => {
    if (!window.confirm(
      'Pull + recreate drift-agent and drift-frontend. The CP will briefly ' +
      'disconnect while the new containers come up. Continue?',
    )) return
    setApplying(true)
    setApplyResult(null)
    setError(null)
    try {
      // The CP will recreate itself mid-request; this fetch may hang or
      // return mid-stream. Either way the new container should come up
      // within ~10s and the next poll will reflect the new digests.
      const res = await fetch(`${apiBase()}/admin/updates/apply`, {
        method: 'POST',
        credentials: 'include',
      })
      // Response is JSON. Success path: helper container dispatched,
      // drift-agent will recreate itself in ~3s and the SPA loses
      // connection briefly. Failure path: error field populated OR
      // helper_returncode non-zero.
      let parsed: any = null
      try { parsed = await res.json() } catch { /* not JSON, skip */ }
      if (parsed?.error) {
        setError(`update failed: ${parsed.error}`)
        if (parsed.helper_output || parsed.pull_output) {
          setApplyResult([parsed.helper_output, parsed.pull_output].filter(Boolean).join('\n').trim())
        }
      } else if (parsed?.helper_returncode != null && parsed.helper_returncode !== 0) {
        setError('updater helper failed to start')
        setApplyResult((parsed.helper_output || '').trim())
      } else if (parsed?.applied?.length) {
        setApplyResult(
          `Recreating ${parsed.applied.join(', ')} in a detached helper. ` +
          `Connection will drop briefly while drift-agent restarts.`,
        )
      } else {
        setApplyResult('Update dispatched.')
      }
    } catch (e) {
      // Connection drop expected when drift-agent recreates itself —
      // treat as success-likely.
      setApplyResult('Connection dropped mid-update — usually normal. Re-polling…')
    } finally {
      setApplying(false)
      // Poll a few times, every 3s, with force=true so each iteration
      // actually re-checks GHCR + the running containers (the cached
      // GET would return drift-agent's pre-restart snapshot for the
      // first few seconds). Stop as soon as a poll succeeds AND the
      // drift-frontend digest has moved off the one we remember — that
      // means the recreate is fully through and the SPA we're running
      // is now stale (the next snapshot reflects the NEW container).
      for (let i = 0; i < 15; i++) {
        await new Promise((r) => setTimeout(r, 3000))
        try {
          await refresh(true)
        } catch { continue }
        // No need to keep polling once we've seen the frontend digest
        // change — the user is about to hit Refresh anyway.
        const live = snapshot?.images.find((s) => s.name === 'drift-frontend')?.current_digest
        if (initialFrontendDigest && live && initialFrontendDigest !== live) break
      }
    }
  }, [refresh, initialFrontendDigest, snapshot])

  const anyUpdate = snapshot?.images.some((i) => i.update_available) ?? false
  // Newest release first; the first one expanded by default if an update
  // is available, collapsed otherwise (room is tight).
  const releases = useMemo(() => snapshot?.releases ?? [], [snapshot])

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ pb: 1, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Stack direction="row" spacing={1} alignItems="center">
          <UpdateIcon fontSize="small" />
          <span>Software updates</span>
        </Stack>
        <IconButton size="small" onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
      </DialogTitle>
      <DialogContent dividers>
        {loading && !snapshot && (
          <Stack direction="row" spacing={1} alignItems="center" sx={{ py: 2 }}>
            <CircularProgress size={16} />
            <Typography variant="body2">Loading…</Typography>
          </Stack>
        )}
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        {needsRefresh && (
          <Alert
            severity="success"
            icon={<RefreshIcon />}
            sx={{ mb: 2 }}
            action={
              <Button color="warning" variant="contained" size="small" onClick={() => window.location.reload()}>
                Refresh page
              </Button>
            }
          >
            <Typography variant="body2" fontWeight={600}>
              Web UI updated — reload to use the new version
            </Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
              The drift-frontend container was recreated. Your current tab is still running the
              previous JS bundle. Refresh to load the new one.
            </Typography>
          </Alert>
        )}
        {applyResult && !needsRefresh && (
          <Alert severity="info" sx={{ mb: 2, whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: '0.7rem' }}>
            {applyResult}
          </Alert>
        )}
        {snapshot && (
          <Stack spacing={2}>
            <Stack direction="row" spacing={2} alignItems="baseline" justifyContent="space-between">
              <Stack direction="row" spacing={1} alignItems="baseline">
                <Typography variant="body2" color="text.secondary">Installed:</Typography>
                <Chip
                  size="small"
                  label={snapshot.install_version || '(dev / unpackaged)'}
                  color={snapshot.install_version && snapshot.install_version !== 'dev' ? 'primary' : 'default'}
                  variant="outlined"
                  sx={{ fontFamily: 'monospace', fontWeight: 600 }}
                />
                {snapshot.bundle_update_available && snapshot.latest_release_tag && (
                  <>
                    <Typography variant="body2" color="text.secondary">→</Typography>
                    <Chip
                      size="small"
                      label={snapshot.latest_release_tag}
                      color="warning"
                      sx={{ fontFamily: 'monospace', fontWeight: 600 }}
                    />
                  </>
                )}
              </Stack>
              <Typography variant="caption" color="text.secondary">
                Last checked: {snapshot.checked_at ? new Date(snapshot.checked_at).toLocaleString() : 'never'}
              </Typography>
            </Stack>

            {snapshot.bundle_update_available && snapshot.latest_release_tag && (
              <Alert severity="warning" icon={<NewReleasesIcon />}>
                <Typography variant="body2" fontWeight={600} sx={{ mb: 0.5 }}>
                  Bundle update available: {snapshot.latest_release_tag}
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 1 }}>
                  The installed tarball is {snapshot.install_version}. Bundle changes (install.sh,
                  docker-compose.yml, config templates) need a re-extract — the in-app "Update now"
                  only handles image updates. On the CP host:
                </Typography>
                <Box
                  sx={{
                    fontFamily: '"JetBrains Mono", monospace',
                    fontSize: '0.75rem',
                    bgcolor: 'rgba(0,0,0,0.3)',
                    p: 1,
                    borderRadius: 0.5,
                    whiteSpace: 'pre',
                    overflowX: 'auto',
                  }}
                >
{`curl -L https://github.com/kidproquo/drift-public/releases/download/${snapshot.latest_release_tag}/drift-deploy-${snapshot.latest_release_tag.replace(/^v/, '')}.tar.gz | tar -xz
cd drift-deploy-${snapshot.latest_release_tag.replace(/^v/, '')}
./install.sh`}
                </Box>
              </Alert>
            )}

            {anyUpdate && releases.length > 0 && (
              <Box
                sx={{
                  border: 1,
                  borderColor: 'warning.main',
                  borderRadius: 1,
                  p: 1.5,
                  bgcolor: 'rgba(255, 152, 0, 0.08)',
                }}
              >
                <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                  <NewReleasesIcon fontSize="small" color="warning" />
                  <Typography variant="subtitle2">
                    What's new in <code style={{ fontFamily: 'monospace' }}>{releases[0].tag}</code>
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ flex: 1 }}>
                    {releases[0].published_at ? new Date(releases[0].published_at).toLocaleDateString() : ''}
                  </Typography>
                </Stack>
                <Box
                  sx={{
                    '& p': { my: 0.6, fontSize: '0.85rem', lineHeight: 1.55 },
                    '& h2': { fontSize: '0.95rem', mt: 1.4, mb: 0.6 },
                    '& h3': { fontSize: '0.9rem', mt: 1.2, mb: 0.5 },
                    '& ul, & ol': { pl: 3, my: 0.6 },
                    '& li': { mb: 0.3, fontSize: '0.85rem' },
                    '& code': {
                      fontFamily: '"JetBrains Mono", monospace',
                      fontSize: '0.78em',
                      bgcolor: 'rgba(255,255,255,0.08)',
                      px: 0.5,
                      py: 0.15,
                      borderRadius: 0.5,
                    },
                    '& pre': {
                      bgcolor: 'rgba(0,0,0,0.3)',
                      p: 1,
                      borderRadius: 0.6,
                      overflowX: 'auto',
                      fontSize: '0.78rem',
                      '& code': { bgcolor: 'transparent', p: 0 },
                    },
                    '& strong': { fontWeight: 600 },
                  }}
                >
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {releases[0].body || '*(no release notes)*'}
                  </ReactMarkdown>
                </Box>
                {releases[0].html_url && (
                  <Link
                    href={releases[0].html_url}
                    target="_blank"
                    rel="noopener"
                    variant="caption"
                    sx={{ display: 'block', mt: 1 }}
                  >
                    View on GitHub →
                  </Link>
                )}
              </Box>
            )}

            {snapshot.images.map((img) => (
              <Box key={img.name} sx={{ border: 1, borderColor: 'divider', borderRadius: 1, p: 1.5 }}>
                <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 0.5 }}>
                  <Stack direction="row" spacing={1} alignItems="center">
                    <Typography variant="subtitle2">{img.name}</Typography>
                    {img.update_available ? (
                      <Chip size="small" icon={<NewReleasesIcon />} label="update available" color="warning" />
                    ) : img.current_digest ? (
                      <Chip size="small" icon={<CheckCircleIcon />} label="up to date" color="success" variant="outlined" />
                    ) : null}
                  </Stack>
                </Stack>
                <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
                  {img.description}
                </Typography>
                {img.error && <Alert severity="warning" sx={{ mt: 0.5 }}>{img.error}</Alert>}
                <Box sx={{ fontFamily: 'monospace', fontSize: '0.7rem', color: 'text.secondary' }}>
                  <div>current:&nbsp;&nbsp;{shortDigest(img.current_digest)}</div>
                  <div>latest:&nbsp;&nbsp;&nbsp;{shortDigest(img.available_digest)}</div>
                </Box>
              </Box>
            ))}
            <Box sx={{ border: 1, borderColor: 'divider', borderRadius: 1, p: 1.5 }}>
              <Typography variant="subtitle2" sx={{ mb: 0.5 }}>edge-agent</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 0.5 }}>
                Bundled with this drift-agent image. Devices auto-update on next check-in.
              </Typography>
              <Box sx={{ fontFamily: 'monospace', fontSize: '0.7rem', color: 'text.secondary' }}>
                <div>version: {snapshot.edge_agent.version ?? '—'}</div>
                <div>sha:&nbsp;&nbsp;&nbsp;&nbsp; {snapshot.edge_agent.sha ?? '—'}</div>
              </Box>
              <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                {snapshot.edge_agent.note}
              </Typography>
            </Box>

            {releases.length > 0 && (
              <Box>
                <Typography variant="subtitle2" sx={{ mb: 1 }}>
                  {anyUpdate ? 'Previous releases' : 'Recent releases'}
                </Typography>
                {/* Skip the newest release when we already showed it in
                    the "What's new" banner above, to avoid duplication. */}
                {(anyUpdate ? releases.slice(1) : releases).map((r, i) => (
                  <Accordion
                    key={r.tag || i}
                    defaultExpanded={false}
                    disableGutters
                    elevation={0}
                    sx={{
                      border: 1,
                      borderColor: 'divider',
                      borderRadius: 1,
                      mb: 1,
                      '&::before': { display: 'none' },
                    }}
                  >
                    <AccordionSummary expandIcon={<ExpandMoreIcon fontSize="small" />}>
                      <Stack direction="row" spacing={1} alignItems="center" sx={{ width: '100%' }}>
                        <Typography variant="body2" fontWeight={600}>{r.tag}</Typography>
                        <Typography variant="caption" color="text.secondary" sx={{ flex: 1 }}>
                          {r.name && r.name !== r.tag ? r.name : ''}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                          {r.published_at ? new Date(r.published_at).toLocaleDateString() : ''}
                        </Typography>
                      </Stack>
                    </AccordionSummary>
                    <AccordionDetails>
                      <Box
                        sx={{
                          '& p': { my: 0.6, fontSize: '0.85rem', lineHeight: 1.55 },
                          '& h2': { fontSize: '0.95rem', mt: 1.4, mb: 0.6 },
                          '& h3': { fontSize: '0.9rem', mt: 1.2, mb: 0.5 },
                          '& ul, & ol': { pl: 3, my: 0.6 },
                          '& li': { mb: 0.3, fontSize: '0.85rem' },
                          '& code': {
                            fontFamily: '"JetBrains Mono", monospace',
                            fontSize: '0.78em',
                            bgcolor: 'rgba(255,255,255,0.06)',
                            px: 0.5,
                            py: 0.15,
                            borderRadius: 0.5,
                          },
                          '& pre': {
                            bgcolor: 'rgba(0,0,0,0.3)',
                            p: 1,
                            borderRadius: 0.6,
                            overflowX: 'auto',
                            fontSize: '0.78rem',
                            '& code': { bgcolor: 'transparent', p: 0 },
                          },
                          '& strong': { fontWeight: 600 },
                        }}
                      >
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{r.body || '*(no notes)*'}</ReactMarkdown>
                      </Box>
                      {r.html_url && (
                        <Link
                          href={r.html_url}
                          target="_blank"
                          rel="noopener"
                          variant="caption"
                          sx={{ display: 'block', mt: 1 }}
                        >
                          View on GitHub →
                        </Link>
                      )}
                    </AccordionDetails>
                  </Accordion>
                ))}
              </Box>
            )}
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button
          startIcon={<RefreshIcon />}
          onClick={() => refresh(true)}
          disabled={loading || applying || needsRefresh}
          size="small"
        >
          Check now
        </Button>
        <Box sx={{ flex: 1 }} />
        <Button onClick={onClose} size="small" disabled={needsRefresh}>Close</Button>
        {needsRefresh ? (
          <Button
            variant="contained"
            color="warning"
            startIcon={<RefreshIcon />}
            onClick={() => window.location.reload()}
            size="small"
          >
            Refresh page
          </Button>
        ) : (
          <Button
            variant="contained"
            startIcon={applying ? <CircularProgress size={14} /> : <UpdateIcon />}
            onClick={apply}
            disabled={applying || !anyUpdate}
            size="small"
          >
            {applying ? 'Applying…' : 'Update now'}
          </Button>
        )}
      </DialogActions>
    </Dialog>
  )
}

function shortDigest(d: string | null): string {
  if (!d) return '—'
  // sha256:abc... → sha256:abcdef0123…
  const colon = d.indexOf(':')
  if (colon < 0) return d
  return `${d.slice(0, colon + 1)}${d.slice(colon + 1, colon + 13)}…`
}
