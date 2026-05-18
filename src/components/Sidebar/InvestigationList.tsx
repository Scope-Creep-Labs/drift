import { useEffect, useState } from 'react'
import {
  Box,
  Button,
  IconButton,
  List,
  ListItemButton,
  ListItemText,
  Stack,
  Tooltip,
  Typography,
} from '@mui/material'
import AddIcon from '@mui/icons-material/Add'
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline'
import InventoryIcon from '@mui/icons-material/Inventory2Outlined'
import KeyIcon from '@mui/icons-material/Key'
import LogoutIcon from '@mui/icons-material/LogoutOutlined'
import { useAuth, isAdmin, isDeploy } from '../../auth/AuthContext'
import { useInvestigationStore } from '../../state/investigationStore'
import { AppModal, type AppModalMode } from '../AppModal'
import { RegistryCredsModal } from '../RegistryCredsModal'
import { deployApi, type App } from '../../lib/deployApi'

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
  const [appsError, setAppsError] = useState<string | null>(null)
  const [modal, setModal] = useState<AppModalMode | null>(null)
  const [credsModalOpen, setCredsModalOpen] = useState(false)
  const auth = useAuth()
  const user = auth.status === 'authenticated' ? auth.user : undefined
  const canDeploy = isDeploy(user)
  const isAdminUser = isAdmin(user)

  const refreshApps = () => {
    setAppsError(null)
    deployApi
      .listApps()
      .then(setApps)
      .catch((e: Error) => setAppsError(e.message))
  }

  useEffect(() => {
    refreshApps()
  }, [])

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
          New investigation
        </Button>
      </Box>

      <List dense sx={{ flex: 1, overflowY: 'auto', px: 0.5 }}>
        {investigations.length === 0 && (
          <Typography variant="caption" color="text.secondary" sx={{ px: 2, py: 1, display: 'block' }}>
            No investigations yet. Ask a question below to begin.
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
          {(apps ?? []).map((a) => (
            <ListItemButton
              key={a.id}
              onClick={canDeploy ? () => setModal({ kind: 'edit', appName: a.name }) : undefined}
              disabled={!canDeploy}
              sx={{ borderRadius: 1, mx: 0.5, mb: 0.2, py: 0.4 }}
            >
              <ListItemText
                primary={a.name}
                primaryTypographyProps={{ fontSize: '0.82rem', noWrap: true }}
              />
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
    </Stack>
  )
}
