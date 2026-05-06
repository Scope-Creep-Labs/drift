import { Paper, Stack, Typography } from '@mui/material'
import ArrowUpwardIcon from '@mui/icons-material/ArrowUpward'
import ArrowDownwardIcon from '@mui/icons-material/ArrowDownward'
import RemoveIcon from '@mui/icons-material/Remove'
import type { MetricBlock as MetricBlockT } from '../../types/blocks'

const trendColor = {
  up: '#ff6b6b',
  down: '#5ad1c1',
  flat: 'text.secondary',
} as const

export function MetricBlock({ block }: { block: MetricBlockT }) {
  const trend = block.trend ?? 'flat'
  const Icon = trend === 'up' ? ArrowUpwardIcon : trend === 'down' ? ArrowDownwardIcon : RemoveIcon
  return (
    <Paper
      variant="outlined"
      sx={{
        px: 2,
        py: 1.5,
        minWidth: 140,
        flex: '0 1 auto',
        borderColor: 'divider',
      }}
    >
      <Typography variant="caption" color="text.secondary" sx={{ textTransform: 'uppercase', letterSpacing: 0.4 }}>
        {block.label}
      </Typography>
      <Stack direction="row" alignItems="baseline" spacing={0.7} sx={{ mt: 0.4 }}>
        <Typography variant="h5" sx={{ fontWeight: 600, fontSize: '1.4rem' }}>
          {block.value}
        </Typography>
        {block.unit && (
          <Typography variant="body2" color="text.secondary">
            {block.unit}
          </Typography>
        )}
        <Icon sx={{ fontSize: 14, ml: 0.4, color: trendColor[trend] }} />
      </Stack>
    </Paper>
  )
}
