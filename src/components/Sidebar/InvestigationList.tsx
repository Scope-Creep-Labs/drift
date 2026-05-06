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
import { useInvestigationStore } from '../../state/investigationStore'

export function InvestigationList() {
  const investigations = useInvestigationStore((s) => s.investigations)
  const activeId = useInvestigationStore((s) => s.activeId)
  const setActive = useInvestigationStore((s) => s.setActive)
  const create = useInvestigationStore((s) => s.createInvestigation)
  const remove = useInvestigationStore((s) => s.deleteInvestigation)

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

      <Box sx={{ p: 1.5, borderTop: 1, borderColor: 'divider' }}>
        <Typography variant="caption" color="text.secondary">
          engine: <code>{import.meta.env.VITE_ENGINE ?? 'mock'}</code>
        </Typography>
      </Box>
    </Stack>
  )
}
