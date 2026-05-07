import { Avatar, Box, Paper, Stack, Typography } from '@mui/material'
import PersonOutlineIcon from '@mui/icons-material/PersonOutline'
import AutoAwesomeIcon from '@mui/icons-material/AutoAwesome'
import type { Turn as TurnT, StreamingTurn } from '../state/investigationStore'
import { BlockRenderer } from './blocks/BlockRenderer'
import { Scratchpad } from './Scratchpad'

type TurnLike = TurnT | (StreamingTurn & { id: string; createdAt: string })

export function Turn({ turn, streaming = false }: { turn: TurnLike; streaming?: boolean }) {
  return (
    <Stack spacing={2.5} sx={{ mb: 4 }}>
      <Stack direction="row" spacing={1.5} alignItems="flex-start">
        <Avatar sx={{ width: 28, height: 28, bgcolor: 'transparent', border: 1, borderColor: 'divider' }}>
          <PersonOutlineIcon fontSize="small" />
        </Avatar>
        <Paper
          variant="outlined"
          sx={{ flex: 1, px: 2, py: 1.4, borderColor: 'divider', bgcolor: 'background.paper' }}
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
          {turn.metadata && (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ display: 'block', mb: 1, textTransform: 'lowercase' }}
            >
              {turn.metadata.engine ?? 'agent'}
              {turn.metadata.usage?.cache_read_input_tokens !== undefined &&
                turn.metadata.usage.cache_read_input_tokens > 0 &&
                ` · cache hit ${turn.metadata.usage.cache_read_input_tokens.toLocaleString()} tok`}
              {turn.metadata.stop_reason && ` · ${turn.metadata.stop_reason}`}
            </Typography>
          )}

          <Scratchpad trace={turn.trace} defaultOpen={streaming} isStreaming={streaming} />

          <BlockRenderer blocks={turn.blocks} contextPrompt={turn.prompt} />

          {streaming && turn.blocks.length === 0 && <StreamingPlaceholder />}

          {turn.error && (
            <Box
              sx={{
                mt: 2,
                p: 1.4,
                border: 1,
                borderColor: 'error.main',
                borderRadius: 1,
                bgcolor: 'rgba(255,107,107,0.08)',
              }}
            >
              <Typography variant="caption" color="error.main">
                {turn.error}
              </Typography>
            </Box>
          )}
        </Box>
      </Stack>
    </Stack>
  )
}

function StreamingPlaceholder() {
  return (
    <Stack direction="row" spacing={0.6} sx={{ mt: 1.5 }}>
      {[0, 1, 2].map((i) => (
        <Box
          key={i}
          sx={{
            width: 6,
            height: 6,
            borderRadius: '50%',
            bgcolor: 'primary.main',
            opacity: 0.5,
            animation: 'driftPulse 1.2s infinite ease-in-out',
            animationDelay: `${i * 0.18}s`,
            '@keyframes driftPulse': {
              '0%, 80%, 100%': { opacity: 0.25, transform: 'scale(0.85)' },
              '40%': { opacity: 1, transform: 'scale(1.2)' },
            },
          }}
        />
      ))}
    </Stack>
  )
}
