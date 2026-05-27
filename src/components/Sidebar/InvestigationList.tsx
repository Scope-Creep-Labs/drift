import { useMemo, useState } from 'react'
import {
  Box,
  Button,
  IconButton,
  InputAdornment,
  List,
  ListItem,
  ListItemButton,
  ListItemText,
  Stack,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import KeyIcon from '@mui/icons-material/Key'
import LockResetIcon from '@mui/icons-material/LockReset'
import LogoutIcon from '@mui/icons-material/LogoutOutlined'
import SearchIcon from '@mui/icons-material/SearchOutlined'
import SystemUpdateAltIcon from '@mui/icons-material/SystemUpdateAltOutlined'
import TerminalIcon from '@mui/icons-material/Terminal'
import { useAuth, isDeploy } from '../../auth/AuthContext'
import { useInvestigationStore } from '../../state/investigationStore'
import { useFleetStore } from '../../state/fleetStore'
import { useTerminalUiStore } from '../../state/terminalUiStore'
import { costForUsage, costForUsageByModel, formatUsd, totalTokens } from '../../lib/pricing'
import { AppModal, type AppModalMode } from '../AppModal'
import { ChangePasswordModal } from '../ChangePasswordModal'
import { SoftwareUpdatesModal } from '../SoftwareUpdatesModal'
import { RegistryCredsModal } from '../RegistryCredsModal'
import { type App, type DeploymentTarget, type Device } from '../../lib/deployApi'
import { TagEditModal } from '../TagEditModal'
import LabelIcon from '@mui/icons-material/LabelOutlined'

// Roll-up of a single app's deployment state across the whole fleet.
// "worst wins" so a single failing target colors the row attention-y.
type AppRollup = {
  total: number             // active (non-removed) deployment count
  state: 'healthy' | 'paused' | 'pending' | 'idle'
  lastTouchedAt: number     // unix-ms; for recency sort
}

function rollupFromDeployments(
  apps: App[],
  deployments: DeploymentTarget[],
): Map<string, AppRollup> {
  const out = new Map<string, AppRollup>()
  for (const a of apps) {
    out.set(a.id, {
      total: 0,
      state: 'idle',
      lastTouchedAt: new Date(a.created_at).getTime(),
    })
  }
  for (const d of deployments) {
    const cur = out.get(d.app_id)
    if (!cur) continue
    const touched = new Date(d.updated_at).getTime()
    if (touched > cur.lastTouchedAt) cur.lastTouchedAt = touched
    // Ignore tombstones for the rollup state — they still bump
    // lastTouchedAt because they're real activity.
    if (d.status === 'removed') continue
    cur.total += 1
    if (d.status === 'paused_retries') cur.state = 'paused'
    else if (cur.state !== 'paused' && d.status !== 'healthy') cur.state = 'pending'
    else if (cur.state === 'idle' && d.status === 'healthy') cur.state = 'healthy'
  }
  return out
}

function StatusDot({ state }: { state: AppRollup['state'] }) {
  const colors: Record<AppRollup['state'], string> = {
    healthy: '#2ea66c',
    paused: '#e15454',
    pending: '#f0a040',
    idle: 'rgba(255,255,255,0.18)',
  }
  const labels: Record<AppRollup['state'], string> = {
    healthy: 'All deployments healthy',
    paused: 'One or more deployments are paused (max retries hit)',
    pending: 'One or more deployments are pending or failing',
    idle: 'No active deployments',
  }
  return (
    <Tooltip title={labels[state]} placement="left">
      <Box
        sx={{
          width: 7,
          height: 7,
          borderRadius: '50%',
          bgcolor: colors[state],
          flexShrink: 0,
          mr: 1,
        }}
      />
    </Tooltip>
  )
}

export function InvestigationList() {
  const investigations = useInvestigationStore((s) => s.investigations)
  const activeId = useInvestigationStore((s) => s.activeId)
  const setActive = useInvestigationStore((s) => s.setActive)
  const create = useInvestigationStore((s) => s.createInvestigation)
  const remove = useInvestigationStore((s) => s.deleteInvestigation)

  // Apps section state. Lives here (not in the global investigation store)
  // because it's narrowly scoped to the sidebar list — and the source of
  // truth is the server.
  const [modal, setModal] = useState<AppModalMode | null>(null)
  const [credsModalOpen, setCredsModalOpen] = useState(false)
  const [passwordModalOpen, setPasswordModalOpen] = useState(false)
  const [updatesModalOpen, setUpdatesModalOpen] = useState(false)
  const [deviceFilter, setDeviceFilter] = useState('')
  const [tagEditDevice, setTagEditDevice] = useState<Device | null>(null)
  // Sidebar splits the previous one-scroll-fits-all layout into 3 tabs so
  // each section gets the full content height instead of fighting for
  // 38% of the sidebar. Defaults to Conversations (matches prior UX).
  const [tab, setTab] = useState<'conversations' | 'devices' | 'apps'>('conversations')
  const openTerminal = useTerminalUiStore((s) => s.open)
  const [filter, setFilter] = useState('')
  const auth = useAuth()
  const user = auth.status === 'authenticated' ? auth.user : undefined
  const usage = auth.status === 'authenticated' ? auth.usage : null
  const canDeploy = isDeploy(user)

  // Apps + deployments come from the shared fleetStore so:
  //  - the sidebar re-renders whenever useInvestigate fires a 'done'
  //    (i.e. immediately after the chat completes a turn that may
  //    have deployed/removed/created something)
  //  - the autocomplete and the sidebar are always in sync
  //  - manual modal saves still refresh by calling refresh() directly
  const apps = useFleetStore((s) => s.apps)
  const devices = useFleetStore((s) => s.devices)
  const deployments = useFleetStore((s) => s.deployments)
  const fleetLoaded = useFleetStore((s) => s.loaded)
  const refreshFleet = useFleetStore((s) => s.refresh)

  // Devices the current user can open a terminal against. Admins see
  // everything; deploy users see only devices in their groups. Sorted
  // alphabetically for a stable list across re-renders.
  const accessibleDevices = useMemo(() => {
    if (!user) return []
    const subset =
      user.role === 'admin'
        ? devices
        : devices.filter((d) => d.group_id && user.groups.includes(d.group_id))
    return [...subset].sort((a, b) => a.name.localeCompare(b.name))
  }, [devices, user])

  // Final list with the search filter applied. Substring match on
  // name + group_id + any tag, so operators can narrow with
  // any of those identifiers ("pi", "edge", "client-z").
  const visibleDevices = useMemo(() => {
    const needle = deviceFilter.trim().toLowerCase()
    if (!needle) return accessibleDevices
    return accessibleDevices.filter(
      (d) =>
        d.name.toLowerCase().includes(needle) ||
        (d.group_id ?? '').toLowerCase().includes(needle) ||
        (d.tags ?? []).some((t) => t.toLowerCase().includes(needle)),
    )
  }, [accessibleDevices, deviceFilter])
  // Until the first refresh completes, render the empty/loading state
  // (matches the prior "apps === null" semantic so the existing JSX
  // branches keep working).
  const appsView: App[] | null = fleetLoaded ? apps : null
  const appsError: string | null = null  // store swallows errors silently

  // Derived: filter + sort the apps for display. Recomputed when any of
  // the inputs change. Sort key is "most recent deployment activity"
  // (lastTouchedAt) so apps you're working on float to the top; the
  // filter is case-insensitive substring on the name.
  const visibleApps = useMemo(() => {
    if (appsView === null) return null
    const rollups = rollupFromDeployments(appsView, deployments)
    const needle = filter.trim().toLowerCase()
    const filtered = needle
      ? appsView.filter((a) => a.name.toLowerCase().includes(needle))
      : appsView.slice()
    filtered.sort((a, b) => {
      const ra = rollups.get(a.id)?.lastTouchedAt ?? 0
      const rb = rollups.get(b.id)?.lastTouchedAt ?? 0
      if (ra !== rb) return rb - ra
      return a.name.localeCompare(b.name)
    })
    return filtered.map((a) => ({ app: a, rollup: rollups.get(a.id)! }))
  }, [appsView, deployments, filter])

  return (
    <Stack
      sx={{
        width: 260,
        flexShrink: 0,
        borderRight: 1,
        borderColor: 'divider',
        height: '100vh',
        bgcolor: 'background.paper',
      }}
    >
      <Box sx={{ px: 2, py: 1.6, borderBottom: 1, borderColor: 'divider' }}>
        <Typography variant="h6" sx={{ fontWeight: 600, letterSpacing: 0.2 }}>
          Drift
        </Typography>
        <Typography variant="caption" color="text.secondary">
          Prompt-native observability
        </Typography>
      </Box>

      <Tabs
        value={tab}
        onChange={(_, v) => setTab(v)}
        variant="fullWidth"
        sx={{
          minHeight: 36,
          borderBottom: 1,
          borderColor: 'divider',
          '& .MuiTab-root': {
            minHeight: 36,
            fontSize: '0.72rem',
            textTransform: 'none',
            letterSpacing: 0.2,
            py: 0.5,
          },
        }}
      >
        <Tab value="conversations" label="Conversations" />
        {canDeploy && (
          <Tab value="devices" label={`Devices · ${accessibleDevices.length}`} />
        )}
        <Tab value="apps" label={`Apps · ${apps.length}`} />
      </Tabs>

      {tab === 'conversations' && (
        <>
          <Box sx={{ p: 1.2 }}>
            <Button
              fullWidth
              variant="outlined"
              startIcon={<AddIcon />}
              onClick={() => create()}
              sx={{ justifyContent: 'flex-start', borderColor: 'divider' }}
            >
              New conversation
            </Button>
          </Box>

          <List dense sx={{ flex: 1, overflowY: 'auto', px: 0.5 }}>
            {investigations.length === 0 && (
              <Typography variant="caption" color="text.secondary" sx={{ px: 2, py: 1, display: 'block' }}>
                No conversations yet. Ask a question below to begin.
              </Typography>
            )}
            {investigations.map((inv) => (
              <ListItemButton
                key={inv.id}
                selected={inv.id === activeId}
                onClick={() => setActive(inv.id)}
                sx={{
                  borderRadius: 1,
                  mx: 0.5,
                  mb: 0.3,
                  '&.Mui-selected': { bgcolor: 'action.selected' },
                }}
              >
                <ListItemText
                  primary={inv.title}
                  secondary={`${inv.turns.length} turn${inv.turns.length === 1 ? '' : 's'}`}
                  primaryTypographyProps={{
                    fontSize: '0.85rem',
                    noWrap: true,
                    fontWeight: inv.id === activeId ? 600 : 400,
                  }}
                  secondaryTypographyProps={{ fontSize: '0.72rem' }}
                />
                <Tooltip title="Delete">
                  <IconButton
                    size="small"
                    onClick={(e) => {
                      e.stopPropagation()
                      remove(inv.id)
                    }}
                  >
                    <DeleteOutlineIcon fontSize="inherit" />
                  </IconButton>
                </Tooltip>
              </ListItemButton>
            ))}
          </List>
        </>
      )}

      <Box
        sx={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0,
          // The Devices/Apps tabs replace the prior maxHeight: 38% bottom
          // section. flex: 1 lets each list use the full available height
          // for its own scroll.
          ...(tab === 'conversations' ? { display: 'none' } : {}),
        }}
      >
        {tab === 'devices' && canDeploy && accessibleDevices.length === 0 && (
          <Box sx={{ px: 2, pt: 3, pb: 2, textAlign: 'center' }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
              No devices yet.
            </Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: 'block' }}>
              Ask Drift to commission one in chat, e.g.:
            </Typography>
            <Typography
              variant="caption"
              sx={{
                display: 'inline-block',
                mt: 1,
                px: 1,
                py: 0.5,
                fontFamily: 'monospace',
                bgcolor: 'action.hover',
                borderRadius: 0.5,
                color: 'text.primary',
              }}
            >
              add device pi-001 to group home
            </Typography>
          </Box>
        )}
        {tab === 'devices' && canDeploy && accessibleDevices.length > 0 && (
          <>
            <Box sx={{ px: 1, pt: 1, pb: 0.4 }}>
              <TextField
                value={deviceFilter}
                onChange={(e) => setDeviceFilter(e.target.value)}
                placeholder="Filter devices…"
                size="small"
                fullWidth
                variant="outlined"
                InputProps={{
                  startAdornment: (
                    <InputAdornment position="start">
                      <SearchIcon sx={{ fontSize: 14, color: 'text.disabled' }} />
                    </InputAdornment>
                  ),
                  sx: { fontSize: '0.78rem', '& input': { py: 0.6 } },
                }}
              />
            </Box>
            <List
              dense
              sx={{ flex: 1, overflowY: 'auto', px: 0.5, py: 0.3 }}
            >
              {deviceFilter && visibleDevices.length === 0 && (
                <Typography
                  variant="caption"
                  color="text.secondary"
                  sx={{ px: 2, display: 'block' }}
                >
                  No devices match "{deviceFilter}".
                </Typography>
              )}
              {visibleDevices.map((d) => {
                const online = d.status === 'online'
                return (
                  // ListItem holds both the primary (clickable terminal
                  // open) and a secondaryAction (tag-edit icon) as
                  // SIBLINGS — keeps each Tooltip's hover zone disjoint,
                  // so they don't stack when the user hovers the icon.
                  <ListItem
                    key={d.id}
                    disablePadding
                    secondaryAction={
                      canDeploy ? (
                        <Tooltip title="Edit tags" placement="left">
                          <IconButton
                            size="small"
                            onClick={() => setTagEditDevice(d)}
                            sx={{ p: 0.3 }}
                          >
                            <LabelIcon sx={{ fontSize: 13, color: 'text.disabled' }} />
                          </IconButton>
                        </Tooltip>
                      ) : null
                    }
                  >
                    <Tooltip
                      title={
                        online
                          ? 'Open terminal'
                          : `${d.status} — terminal disabled until next check-in`
                      }
                      placement="right"
                    >
                      {/* span wraps disabled button so the tooltip still fires */}
                      <span style={{ width: '100%' }}>
                        <ListItemButton
                          onClick={online ? () => openTerminal(d.name) : undefined}
                          disabled={!online}
                          sx={{
                            borderRadius: 1,
                            mx: 0.5,
                            py: 0.4,
                            // Right-pad to leave room for the secondaryAction
                            // icon (~32px wide) so it doesn't overlap the
                            // device name on long rows.
                            pr: canDeploy ? 5 : 4,
                            '&.Mui-disabled': { opacity: 0.55 },
                          }}
                        >
                          <Box
                            sx={{
                              width: 6,
                              height: 6,
                              borderRadius: '50%',
                              mr: 1,
                              flexShrink: 0,
                              bgcolor: online
                                ? 'success.main'
                                : d.status === 'offline'
                                  ? 'error.main'
                                  : 'warning.main',
                            }}
                          />
                          <ListItemText
                            primary={d.name}
                            secondary={
                              <Box component="span" sx={{ display: 'flex', alignItems: 'center', gap: 0.4, flexWrap: 'wrap' }}>
                                {d.group_id && (
                                  <Box component="span" sx={{ fontSize: '0.66rem', color: 'text.secondary' }}>
                                    {d.group_id}
                                  </Box>
                                )}
                                {(d.tags ?? []).map((t) => (
                                  <Box
                                    key={t}
                                    component="span"
                                    sx={{
                                      fontSize: '0.6rem',
                                      fontFamily: '"JetBrains Mono", monospace',
                                      bgcolor: 'action.selected',
                                      color: 'text.primary',
                                      px: 0.5,
                                      py: 0.05,
                                      borderRadius: 0.5,
                                      lineHeight: 1.4,
                                    }}
                                  >
                                    {t}
                                  </Box>
                                ))}
                              </Box>
                            }
                            primaryTypographyProps={{
                              sx: { fontSize: '0.78rem', fontWeight: 500 },
                            }}
                            secondaryTypographyProps={{ component: 'div' }}
                          />
                          <TerminalIcon
                            sx={{ fontSize: 14, color: 'text.disabled', ml: 1 }}
                          />
                        </ListItemButton>
                      </span>
                    </Tooltip>
                  </ListItem>
                )
              })}
            </List>
          </>
        )}

        {tab === 'apps' && (
          <Stack
            direction="row"
            alignItems="center"
            justifyContent="space-between"
            sx={{ px: 1.6, pt: 1, pb: 0.4 }}
          >
            {canDeploy && (
              <Tooltip title="New app">
                <Button
                  size="small"
                  startIcon={<AddIcon sx={{ fontSize: 14 }} />}
                  onClick={() => setModal({ kind: 'create' })}
                  sx={{ fontSize: '0.72rem', textTransform: 'none' }}
                >
                  New app
                </Button>
              </Tooltip>
            )}
          </Stack>
        )}

        {tab === 'apps' && appsView !== null && apps.length > 5 && (
          <Box sx={{ px: 1, pb: 0.4 }}>
            <TextField
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="Filter apps…"
              size="small"
              fullWidth
              variant="outlined"
              InputProps={{
                startAdornment: (
                  <InputAdornment position="start">
                    <SearchIcon sx={{ fontSize: 14, color: 'text.disabled' }} />
                  </InputAdornment>
                ),
                sx: { fontSize: '0.78rem', '& input': { py: 0.6 } },
              }}
            />
          </Box>
        )}

        {tab === 'apps' && <List dense sx={{ flex: 1, overflowY: 'auto', px: 0.5, py: 0.3 }}>
          {appsError && (
            <Typography variant="caption" color="error.main" sx={{ px: 2, display: 'block' }}>
              {appsError}
            </Typography>
          )}
          {appsView !== null && apps.length === 0 && !appsError && (
            <Typography variant="caption" color="text.secondary" sx={{ px: 2, display: 'block' }}>
              No apps yet.
            </Typography>
          )}
          {visibleApps !== null && visibleApps.length === 0 && filter && (
            <Typography variant="caption" color="text.secondary" sx={{ px: 2, display: 'block' }}>
              No apps match "{filter}".
            </Typography>
          )}
          {(visibleApps ?? []).map(({ app: a, rollup }) => (
            <ListItemButton
              key={a.id}
              onClick={canDeploy ? () => setModal({ kind: 'edit', appName: a.name }) : undefined}
              disabled={!canDeploy}
              sx={{
                borderRadius: 1,
                mx: 0.5,
                mb: 0.2,
                py: 0.4,
                display: 'flex',
                alignItems: 'center',
              }}
            >
              <StatusDot state={rollup.state} />
              <ListItemText
                primary={a.name}
                primaryTypographyProps={{ fontSize: '0.82rem', noWrap: true }}
                sx={{ flex: 1, minWidth: 0 }}
              />
              {rollup.total > 0 && (
                <Typography
                  variant="caption"
                  sx={{
                    fontSize: '0.66rem',
                    color: 'text.disabled',
                    ml: 0.8,
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  ×{rollup.total}
                </Typography>
              )}
            </ListItemButton>
          ))}
        </List>}
      </Box>

      <Box
        sx={{
          px: 1.5,
          py: 1,
          borderTop: 1,
          borderColor: 'divider',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 1,
        }}
      >
        <Box sx={{ minWidth: 0, flex: 1 }}>
          {user && (
            <>
              <Typography
                variant="caption"
                sx={{ fontSize: '0.75rem', fontWeight: 600, display: 'block', lineHeight: 1.2 }}
                noWrap
              >
                {user.username}
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.66rem', display: 'block' }}>
                {user.role}
                {user.role !== 'admin' && user.groups.length > 0 && ` · ${user.groups.join(', ')}`}
              </Typography>
              {usage && usage.turns > 0 && (() => {
                const tok = totalTokens(usage)
                // Per-model cost using the right $/M-token per provider.
                // Fall back to single-model pricing only when the backend
                // didn't return a breakdown (older clients / pre-litellm).
                const cost = usage.models && Object.keys(usage.models).length > 0
                  ? costForUsageByModel(usage.models)
                  : costForUsage(usage)
                const modelEntries = Object.entries(usage.models ?? {})
                  .map(([name, u]) => ({
                    name,
                    tok:
                      (u.input_tokens ?? 0) +
                      (u.output_tokens ?? 0) +
                      (u.cache_read_input_tokens ?? 0) +
                      (u.cache_creation_input_tokens ?? 0),
                    usd: costForUsage(u, name),
                  }))
                  .sort((a, b) => b.usd - a.usd)
                return (
                  <Tooltip
                    placement="top"
                    title={
                      <Box sx={{ fontSize: '0.7rem', lineHeight: 1.5 }}>
                        <div style={{ fontWeight: 600, marginBottom: 2 }}>last {usage.window_days}d</div>
                        {modelEntries.length > 0 && (
                          <Box sx={{ borderBottom: '1px solid rgba(255,255,255,0.12)', pb: 0.4, mb: 0.4 }}>
                            {modelEntries.map((m) => (
                              <div key={m.name}>
                                {m.name}: {(m.tok / 1000).toFixed(1)}k tok · {formatUsd(m.usd)}
                              </div>
                            ))}
                          </Box>
                        )}
                        <div>in: {usage.input_tokens.toLocaleString()}</div>
                        <div>out: {usage.output_tokens.toLocaleString()}</div>
                        <div>cache read: {usage.cache_read_input_tokens.toLocaleString()}</div>
                        <div>cache write: {usage.cache_creation_input_tokens.toLocaleString()}</div>
                        <div>{usage.turns} turn{usage.turns === 1 ? '' : 's'}</div>
                      </Box>
                    }
                  >
                    <Typography
                      variant="caption"
                      color="text.disabled"
                      sx={{ fontSize: '0.62rem', display: 'block', mt: 0.2, cursor: 'help' }}
                    >
                      {tok >= 1000 ? `${(tok / 1000).toFixed(1)}k` : tok} tok · {formatUsd(cost)}
                    </Typography>
                  </Tooltip>
                )
              })()}
            </>
          )}
        </Box>
        <Stack direction="row" spacing={0.2}>
          {user?.role === 'admin' && (
            <Tooltip title="Software updates">
              <IconButton size="small" onClick={() => setUpdatesModalOpen(true)} sx={{ p: 0.4 }}>
                <SystemUpdateAltIcon sx={{ fontSize: 14 }} />
              </IconButton>
            </Tooltip>
          )}
          {isDeploy(user) && (
            <Tooltip title="Registry credentials">
              <IconButton size="small" onClick={() => setCredsModalOpen(true)} sx={{ p: 0.4 }}>
                <KeyIcon sx={{ fontSize: 14 }} />
              </IconButton>
            </Tooltip>
          )}
          <Tooltip title="Change password">
            <IconButton size="small" onClick={() => setPasswordModalOpen(true)} sx={{ p: 0.4 }}>
              <LockResetIcon sx={{ fontSize: 14 }} />
            </IconButton>
          </Tooltip>
          <Tooltip title="Sign out">
            <IconButton size="small" onClick={() => auth.logout()} sx={{ p: 0.4 }}>
              <LogoutIcon sx={{ fontSize: 14 }} />
            </IconButton>
          </Tooltip>
        </Stack>
      </Box>

      {modal && (
        <AppModal
          open
          mode={modal}
          onClose={() => setModal(null)}
          onSaved={refreshFleet}
        />
      )}

      <RegistryCredsModal open={credsModalOpen} onClose={() => setCredsModalOpen(false)} />
      <ChangePasswordModal open={passwordModalOpen} onClose={() => setPasswordModalOpen(false)} />
      <SoftwareUpdatesModal open={updatesModalOpen} onClose={() => setUpdatesModalOpen(false)} />
      <TagEditModal
        open={tagEditDevice !== null}
        device={tagEditDevice}
        onClose={() => setTagEditDevice(null)}
        onSaved={() => {
          // refreshFleet pulls the canonical device list from the API
          // and updates the sidebar store, which causes our row to
          // re-render with the new tags. The modal closes itself.
          refreshFleet?.()
        }}
      />
      {/* TerminalModal is mounted at the App root via useTerminalUiStore
          — any surface (sidebar row, chat action card) can open it. */}
    </Stack>
  )
}
