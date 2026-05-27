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
  has_bundle_changes: boolean
}

type Snapshot = {
  checked_at: string | null
  install_version: string | null
  running_version: string | null
  latest_release_tag: string | null
  has_newer_release: boolean
  image_update_pending: boolean
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
    // Force a fresh GHCR + release poll on open so the operator sees
    // current state immediately — the cached snapshot can be up to 15
    // minutes stale (the background poll interval). The cost is a
    // single round-trip to GHCR + the GitHub Releases API which is
    // well within budget.
    if (open) refresh(true)
  }, [open, refresh])

  const apply = useCallback(async () => {
    if (!window.confirm(
      'Pull + recreate drift-agent and drift-frontend. The CP will briefly ' +
      'disconnect while the new containers come up. Continue?',
    )) return
    setApplying(true)
    setApplyResult(null)
    setError(null)

    // Step 1 — dispatch the apply. drift-agent returns the JSON
    // synchronously (helper is detached); we can detect immediate
    // failures before drift-agent's own self-recreate kicks in.
    let parsed: any = null
    try {
      const res = await fetch(`${apiBase()}/admin/updates/apply`, {
        method: 'POST',
        credentials: 'include',
      })
      parsed = await res.json().catch(() => null)
    } catch {
      // Network error before any response — could mean drift-agent died
      // before responding (unlikely but possible). Treat as
      // success-likely; the polling below confirms or contradicts.
    }

    if (parsed?.error) {
      setError(`update failed: ${parsed.error}`)
      if (parsed.helper_output || parsed.pull_output) {
        setApplyResult([parsed.helper_output, parsed.pull_output].filter(Boolean).join('\n').trim())
      }
      setApplying(false)
      return
    }
    if (parsed?.helper_returncode != null && parsed.helper_returncode !== 0) {
      setError('updater helper failed to start')
      setApplyResult((parsed.helper_output || '').trim())
      setApplying(false)
      return
    }

    // Success path. Show one friendly message and KEEP `applying`=true
    // through the entire poll-back window so the button stays in
    // "Applying…" state. The needsRefresh banner takes over once the
    // recreate is detected; we don't reset applyResult after that
    // (the banner is the source of truth).
    setApplyResult(
      'Recreating containers in a detached helper. ' +
      'The CP will briefly disconnect — this page will prompt you to refresh once it’s back.',
    )

    // Step 2 — poll up to 45s with force=true, swallowing connection
    // errors silently (502 from Caddy while drift-agent restarts is
    // expected, not a failure the user needs to see). Stop as soon as
    // the drift-frontend digest moves off the one we remembered when
    // the modal opened — that means the recreate is through.
    for (let i = 0; i < 15; i++) {
      await new Promise((r) => setTimeout(r, 3000))
      try {
        const res = await fetch(`${apiBase()}/admin/updates/check`, {
          method: 'POST',
          credentials: 'include',
        })
        if (!res.ok) continue  // 502/etc during restart — silent
        const data: Snapshot = await res.json()
        setSnapshot(data)
        // Bail out as soon as the recreate completes.
        const live = data.images.find((s) => s.name === 'drift-frontend')?.current_digest
        if (initialFrontendDigest && live && initialFrontendDigest !== live) break
      } catch {
        // network unreachable during restart — keep polling silently
        continue
      }
    }

    setApplying(false)
  }, [initialFrontendDigest])

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
                <Typography variant="body2" color="text.secondary">Running:</Typography>
                <Chip
                  size="small"
                  // Prefer running_version (read from image LABEL) over
                  // install_version (the tarball install.sh recorded).
                  // After an Update now succeeds, running_version
                  // reflects the new image immediately — install_version
                  // only changes on re-install.
                  label={snapshot.running_version || snapshot.install_version || '(unknown)'}
                  color={
                    snapshot.running_version && snapshot.running_version !== 'dev'
                      ? 'primary' : 'default'
                  }
                  variant="outlined"
                  sx={{ fontFamily: 'monospace', fontWeight: 600 }}
                />
                {snapshot.has_newer_release && snapshot.latest_release_tag && (
                  <>
                    <Typography variant="body2" color="text.secondary">→</Typography>
                    <Chip
                      size="small"
                      label={snapshot.latest_release_tag}
                      // Warning when bundle re-install is needed; info
                      // when only an image update is pending (fully
                      // addressable via the Update now button).
                      color={snapshot.bundle_update_available ? 'warning' : 'info'}
                      sx={{ fontFamily: 'monospace', fontWeight: 600 }}
                    />
                  </>
                )}
              </Stack>
              <Stack direction="row" spacing={0.6} alignItems="center">
                {/* Spinner visible while a check is in flight (initial
                    open OR explicit Check-now click), then disappears
                    once the snapshot updates. Inline so the timestamp
                    text doesn't shift around. */}
                {loading && <CircularProgress size={10} thickness={5} />}
                <Typography variant="caption" color="text.secondary">
                  {loading
                    ? 'Checking…'
                    : `Last checked: ${snapshot.checked_at ? new Date(snapshot.checked_at).toLocaleString() : 'never'}`}
                </Typography>
              </Stack>
            </Stack>

            {/* When the bundle (install.sh) version differs from what's
                running, surface it on a separate line so the operator
                can see why a bundle re-install might still be pending
                even though the images are current. */}
            {snapshot.install_version &&
              snapshot.running_version &&
              snapshot.install_version !== snapshot.running_version && (
              <Typography variant="caption" color="text.secondary" sx={{ mt: -1 }}>
                Bundle (install.sh): {snapshot.install_version}
              </Typography>
            )}

            {anyUpdate && !snapshot.has_newer_release && (
              <Alert severity="info">
                <Typography variant="body2" fontWeight={600} sx={{ mb: 0.3 }}>
                  Image updates available (no published release yet)
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  drift-agent and/or drift-frontend on GHCR are newer than what's
                  running here, but there's no tarball release describing the change.
                  Clicking Update now will pull and recreate them.
                </Typography>
              </Alert>
            )}

            {snapshot.bundle_update_available && snapshot.latest_release_tag && (
              <Alert
                severity="warning"
                icon={<NewReleasesIcon />}
                action={
                  <Button
                    color="warning"
                    variant="contained"
                    size="small"
                    href={`https://github.com/kidproquo/drift-public/releases/tag/${snapshot.latest_release_tag}`}
                    target="_blank"
                    rel="noopener"
                  >
                    View release
                  </Button>
                }
              >
                <Typography variant="body2" fontWeight={600} sx={{ mb: 0.3 }}>
                  Manual upgrade required for {snapshot.latest_release_tag}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  This release ships changes to install.sh / compose / config templates that the
                  in-app updater can't safely apply (new env vars or compose changes can put the
                  stack into a partial-config state). The Update now button is disabled until you
                  re-extract the tarball on the CP host and run install.sh — see the release page.
                </Typography>
              </Alert>
            )}

            {/*
              "What's new" shows whenever a release newer than the
              installed bundle exists — applies to both image-only and
              bundle releases. (The bundle banner above already tells
              the operator separately if they need to re-install for
              non-image changes.)
            */}
            {snapshot.has_newer_release && releases.length > 0 && (
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
                  <Chip
                    size="small"
                    label={releases[0].has_bundle_changes ? 'bundle' : 'image-only'}
                    color={releases[0].has_bundle_changes ? 'warning' : 'info'}
                    variant="outlined"
                    sx={{ height: 18, fontSize: '0.65rem' }}
                  />
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

                {/* edge-agent script ships INSIDE drift-agent's image
                    (/opt/edge-agent/), so it's logically a sub-component.
                    Render its details only inside the drift-agent card. */}
                {img.name === 'drift-agent' && (
                  <Box
                    sx={{
                      mt: 1,
                      pt: 1,
                      borderTop: 1,
                      borderColor: 'divider',
                    }}
                  >
                    <Typography variant="caption" fontWeight={600} sx={{ display: 'block', mb: 0.3 }}>
                      edge-agent (bundled)
                    </Typography>
                    <Box sx={{ fontFamily: 'monospace', fontSize: '0.7rem', color: 'text.secondary' }}>
                      <div>version: {snapshot.edge_agent.version ?? '—'}</div>
                      <div>sha:&nbsp;&nbsp;&nbsp;&nbsp; {snapshot.edge_agent.sha ?? '—'}</div>
                    </Box>
                    <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 0.5 }}>
                      {snapshot.edge_agent.note}
                    </Typography>
                  </Box>
                )}
              </Box>
            ))}

            {releases.length > 0 && (
              <Box>
                <Typography variant="subtitle2" sx={{ mb: 1 }}>
                  {snapshot.has_newer_release ? 'Previous releases' : 'Recent releases'}
                </Typography>
                {/* Skip the newest release when the "What's new" banner
                    above already shows it, to avoid duplication. */}
                {(snapshot.has_newer_release ? releases.slice(1) : releases).map((r, i) => (
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
                        <Chip
                          size="small"
                          label={r.has_bundle_changes ? 'bundle' : 'image-only'}
                          color={r.has_bundle_changes ? 'warning' : 'info'}
                          variant="outlined"
                          sx={{ height: 18, fontSize: '0.65rem' }}
                        />
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
            // Disabled when a bundle update is pending: bundle changes
            // (install.sh, compose, configs) can't be partially applied
            // by `docker compose up` alone — pulling new images against
            // an old compose can leave the stack misconfigured. Force
            // the manual re-install path; the bundle banner above tells
            // the operator exactly what to do.
            disabled={applying || !anyUpdate || snapshot?.bundle_update_available}
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
