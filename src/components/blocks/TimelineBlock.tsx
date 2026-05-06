import { Box, Paper, Stack, Typography } from '@mui/material'
import type { TimelineBlock as TimelineBlockT } from '../../types/blocks'

const sevColor = {
  info: '#7c9cff',
  warn: '#ff9f43',
  error: '#ff6b6b',
} as const

export function TimelineBlock({ block }: { block: TimelineBlockT }) {
  return (
    <Paper variant="outlined" sx={{ borderColor: 'divider', p: 2 }}>
      {block.title && (
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ textTransform: 'uppercase', letterSpacing: 0.4, mb: 1.2, display: 'block' }}
        >
          {block.title}
        </Typography>
      )}
      <Stack spacing={1.1}>
        {block.events.map((e, i) => {
          const color = sevColor[e.severity ?? 'info']
          return (
            <Stack key={i} direction="row" spacing={1.5} alignItems="center">
              <Box
                sx={{
                  width: 8,
                  height: 8,
                  borderRadius: '50%',
                  bgcolor: color,
                  boxShadow: `0 0 0 3px ${color}22`,
                  flexShrink: 0,
                }}
              />
              <Typography
                variant="caption"
                sx={{
                  fontFamily: '"JetBrains Mono", monospace',
                  color: 'text.secondary',
                  minWidth: 78,
                }}
              >
                {new Date(e.ts).toISOString().slice(11, 19)}
              </Typography>
              <Typography variant="body2">{e.label}</Typography>
            </Stack>
          )
        })}
      </Stack>
    </Paper>
  )
}
