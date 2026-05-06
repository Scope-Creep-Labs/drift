import { Avatar, Box, Paper, Stack, Typography } from '@mui/material'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome'
import type { Turn as TurnT } from '../state/investigationStore'
import { BlockRenderer } from './blocks/BlockRenderer'

export function Turn({ turn }: { turn: TurnT }) {
  return (
    <Stack spacing={2.5} sx={{ mb: 4 }}>
      <Stack direction="row" spacing={1.5} alignItems="flex-start">
        <Avatar sx={{ width: 28, height: 28, bgcolor: 'transparent', border: 1, borderColor: 'divider' }}>
          <PersonOutlineIcon fontSize="small" />
        </Avatar>
        <Paper
          variant="outlined"
          sx={{
            flex: 1,
            px: 2,
            py: 1.4,
            borderColor: 'divider',
            bgcolor: 'background.paper',
          }}
        >
          <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap' }}>
            {turn.prompt}
          </Typography>
        </Paper>
      </Stack>

      <Stack direction="row" spacing={1.5} alignItems="flex-start">
        <Avatar
          sx={{
            width: 28,
            height: 28,
            bgcolor: 'transparent',
            border: 1,
            borderColor: 'primary.main',
            color: 'primary.main',
          }}
        >
          <AutoAwesomeIcon fontSize="small" />
        </Avatar>
        <Box sx={{ flex: 1, minWidth: 0 }}>
          {turn.response.metadata && (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ display: 'block', mb: 1, textTransform: 'lowercase' }}
            >
              {turn.response.metadata.engine}
              {turn.response.metadata.confidence !== undefined &&
                ` · confidence ${(turn.response.metadata.confidence * 100).toFixed(0)}%`}
            </Typography>
          )}
          <BlockRenderer blocks={turn.response.blocks} contextPrompt={turn.prompt} />
        </Box>
      </Stack>
    </Stack>
  )
}
