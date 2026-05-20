import { useEffect, useMemo, useState } from 'react'
import {
  Box,
  Button,
  IconButton,
  InputAdornment,
  List,
  ListItemButton,
  ListItemText,
  Stack,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import InventoryIcon from '@mui/icons-material/Inventory2Outlined'
import KeyIcon from '@mui/icons-material/Key'
import LockResetIcon from '@mui/icons-material/LockReset'
import LogoutIcon from '@mui/icons-material/LogoutOutlined'
import SearchIcon from '@mui/icons-material/SearchOutlined'
import { useAuth, isAdmin, isDeploy } from '../../auth/AuthContext'
import { useInvestigationStore } from '../../state/investigationStore'
import { AppModal, type AppModalMode } from '../AppModal'
import { ChangePasswordModal } from '../ChangePasswordModal'
import { RegistryCredsModal } from '../RegistryCredsModal'
import { deployApi, type App, type DeploymentTarget } from '../../lib/deployApi'

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
  const [apps, setApps] = useState<App[] | null>(null)
  const [deployments, setDeployments] = useState<DeploymentTarget[]>([])
  const [appsError, setAppsError] = useState<string | null>(null)
  const [modal, setModal] = useState<AppModalMode | null>(null)
  const [credsModalOpen, setCredsModalOpen] = useState(false)
  const [passwordModalOpen, setPasswordModalOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const auth = useAuth()
  const user = auth.status === 'authenticated' ? auth.user : undefined
  const canDeploy = isDeploy(user)
  const isAdminUser = isAdmin(user)

  const refreshApps = () => {
    setAppsError(null)
    // Parallel fetch of apps + deployments — both feed the sidebar's
    // rollup view (status badges + recency sort).
    Promise.all([deployApi.listApps(), deployApi.listDeployments()])
      .then(([a, d]) => {
        setApps(a)
        setDeployments(d)
      })
      .catch((e: Error) => setAppsError(e.message))
  }

  useEffect(() => {
    refreshApps()
  }, [])

  // Derived: filter + sort the apps for display. Recomputed when any of
  // the inputs change. Sort key is "most recent deployment activity"
  // (lastTouchedAt) so apps you're working on float to the top; the
  // filter is case-insensitive substring on the name.
  const visibleApps = useMemo(() => {
    if (apps === null) return null
    const rollups = rollupFromDeployments(apps, deployments)
    const needle = filter.trim().toLowerCase()
    const filtered = needle
      ? apps.filter((a) => a.name.toLowerCase().includes(needle))
      : apps.slice()
    filtered.sort((a, b) => {
      const ra = rollups.get(a.id)?.lastTouchedAt ?? 0
      const rb = rollups.get(b.id)?.lastTouchedAt ?? 0
      if (ra !== rb) return rb - ra
      return a.name.localeCompare(b.name)
    })
    return filtered.map((a) => ({ app: a, rollup: rollups.get(a.id)! }))
  }, [apps, deployments, filter])

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

      <Box
        sx={{
          borderTop: 1,
          borderColor: 'divider',
          maxHeight: '38%',
          display: 'flex',
          flexDirection: 'column',
          minHeight: 0,
        }}
      >
        <Stack
          direction="row"
          alignItems="center"
          justifyContent="space-between"
          sx={{ px: 1.6, pt: 1.2, pb: 0.6 }}
        >
          <Stack direction="row" alignItems="center" spacing={0.8}>
            <InventoryIcon sx={{ fontSize: 14, color: 'text.secondary' }} />
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ textTransform: 'uppercase', letterSpacing: 0.5, fontWeight: 600 }}
            >
              Apps
            </Typography>
          </Stack>
          {canDeploy && (
            <Tooltip title="New app">
              <IconButton
                size="small"
                onClick={() => setModal({ kind: 'create' })}
                sx={{ p: 0.3 }}
              >
                <AddIcon sx={{ fontSize: 16 }} />
              </IconButton>
            </Tooltip>
          )}
        </Stack>

        {apps !== null && apps.length > 5 && (
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

        <List dense sx={{ flex: 1, overflowY: 'auto', px: 0.5, py: 0.3 }}>
          {appsError && (
            <Typography variant="caption" color="error.main" sx={{ px: 2, display: 'block' }}>
              {appsError}
            </Typography>
          )}
          {apps !== null && apps.length === 0 && !appsError && (
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
        </List>
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
              <Typography variant="caption" color="text.secondary" sx={{ fontSize: '0.66rem' }}>
                {user.role}
                {user.role !== 'admin' && user.groups.length > 0 && ` · ${user.groups.join(', ')}`}
              </Typography>
            </>
          )}
        </Box>
        <Stack direction="row" spacing={0.2}>
          {isAdminUser && (
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
          onSaved={refreshApps}
        />
      )}

      <RegistryCredsModal open={credsModalOpen} onClose={() => setCredsModalOpen(false)} />
      <ChangePasswordModal open={passwordModalOpen} onClose={() => setPasswordModalOpen(false)} />
    </Stack>
  )
}
